#!/usr/bin/env python3
"""
tts.py - مولّد أصوات قوي باستخدام Microsoft Edge TTS (مكتبة edge-tts).

المزايا:
  * إدخال النص من سطر الأوامر أو من ملف أو من stdin.
  * اختيار تلقائي لصوت عربي عند اكتشاف نص عربي.
  * التحكم في السرعة (rate) والطبقة (pitch) ومستوى الصوت (volume).
  * توليد ملف ترجمة SRT متزامن مع الصوت.
  * تقطيع النصوص الطويلة إلى أجزاء ودمجها في ملف واحد.
  * وضع الدُفعات (batch): توليد عشرات الملفات من ملف نصي أو CSV أو JSON بالتوازي.
  * إعادة المحاولة التلقائية مع تراجع أسّي عند أخطاء الشبكة.
  * عرض الأصوات المتاحة وتصفيتها حسب اللغة أو الجنس.
  * دعم البروكسي وحزمة شهادات CA مخصصة.

أمثلة:
  python tts.py "مرحبا بك في عالم الذكاء الاصطناعي" -o hello.mp3
  python tts.py -f article.txt -v ar-EG-SalmaNeural --rate +10% --srt
  echo "Hello world" | python tts.py -o hello.mp3
  python tts.py --list-voices --lang ar
  python tts.py --batch lines.txt --out-dir voices/ --jobs 4
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import ssl
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import edge_tts
except ImportError:  # pragma: no cover
    sys.exit("مكتبة edge-tts غير مثبتة. ثبّتها بالأمر:  pip install -r requirements.txt")

log = logging.getLogger("tts")

DEFAULT_VOICE_AR = "ar-SA-HamedNeural"
DEFAULT_VOICE_EN = "en-US-AndrewMultilingualNeural"

# أسماء مختصرة لأصوات شائعة
VOICE_ALIASES = {
    "hamed": "ar-SA-HamedNeural",
    "zariyah": "ar-SA-ZariyahNeural",
    "salma": "ar-EG-SalmaNeural",
    "shakir": "ar-EG-ShakirNeural",
    "fatima": "ar-AE-FatimaNeural",
    "hamdan": "ar-AE-HamdanNeural",
    "amina": "ar-DZ-AminaNeural",
    "ismael": "ar-DZ-IsmaelNeural",
    "jamal": "ar-MA-JamalNeural",
    "mouna": "ar-MA-MounaNeural",
    "andrew": "en-US-AndrewMultilingualNeural",
    "emma": "en-US-EmmaMultilingualNeural",
    "ava": "en-US-AvaMultilingualNeural",
    "brian": "en-US-BrianMultilingualNeural",
}

ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
SENTENCE_END_RE = re.compile(r"(?<=[.!?؟。\n…؛;])\s+")
INVALID_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

MAX_CHUNK_CHARS = 3000


# --------------------------------------------------------------------------- #
# أدوات مساعدة
# --------------------------------------------------------------------------- #
def resolve_voice(voice: str | None, text: str) -> str:
    """يعيد اسم الصوت الكامل؛ ويختار صوتاً تلقائياً حسب لغة النص إن لم يُحدَّد."""
    if voice:
        return VOICE_ALIASES.get(voice.lower(), voice)
    return DEFAULT_VOICE_AR if ARABIC_RE.search(text) else DEFAULT_VOICE_EN


def normalize_signed(value: str, unit: str, name: str) -> str:
    """يحوّل قيماً مثل '10' أو '-5%' إلى الصيغة التي يتطلبها edge-tts: '+10%'."""
    v = str(value).strip().replace(" ", "")
    if v.endswith(unit):
        v = v[: -len(unit)]
    if not re.fullmatch(r"[+-]?\d+", v):
        raise argparse.ArgumentTypeError(f"قيمة {name} غير صالحة: {value!r} (مثال: +10{unit})")
    if v[0] not in "+-":
        v = "+" + v
    return f"{v}{unit}"


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """يقسّم النص الطويل إلى أجزاء عند حدود الجمل دون تجاوز max_chars."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) <= max_chars:
        return [text] if text else []

    chunks: list[str] = []
    current = ""
    for sentence in SENTENCE_END_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        # جملة أطول من الحد: قسّمها على الكلمات
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if len(current) + len(sentence) + 1 > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def safe_filename(name: str, max_len: int = 60) -> str:
    name = INVALID_FILENAME_RE.sub("_", name).strip(" ._")
    name = re.sub(r"\s+", "_", name)
    return name[:max_len] or "audio"


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def configure_ssl(ca_bundle: str | None) -> None:
    """edge-tts يستخدم شهادات certifi فقط؛ نضيف إليها حزمة CA مخصصة إن وُجدت
    (مفيد خلف بروكسي شركات أو بيئات معزولة)."""
    if not ca_bundle:
        return
    if not os.path.isfile(ca_bundle):
        log.warning("ملف شهادات CA غير موجود: %s", ca_bundle)
        return
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.load_verify_locations(cafile=ca_bundle)
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذّر تحميل شهادات CA (%s): %s", ca_bundle, exc)
        return
    for mod_name in ("communicate", "voices"):
        mod = getattr(edge_tts, mod_name, None)
        if mod is not None and hasattr(mod, "_SSL_CTX"):
            mod._SSL_CTX = ctx
    log.debug("تم تحميل شهادات CA إضافية من %s", ca_bundle)


# --------------------------------------------------------------------------- #
# التوليد
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    text: str
    output: Path
    voice: str
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    srt: bool = False
    word_srt: bool = False


@dataclass
class Result:
    job: Job
    ok: bool
    seconds: float = 0.0
    error: str = ""
    bytes_written: int = 0
    srt_path: Path | None = None


@dataclass
class Settings:
    retries: int = 4
    proxy: str | None = None
    connect_timeout: int = 15
    receive_timeout: int = 90
    max_chunk: int = MAX_CHUNK_CHARS
    overwrite: bool = True
    extra: dict = field(default_factory=dict)


async def synthesize_chunk(text: str, job: Job, s: Settings, offset: float):
    """يولّد جزءاً واحداً مع إعادة المحاولة. يعيد (bytes, cues, duration)."""
    boundary = "WordBoundary" if job.word_srt else "SentenceBoundary"
    last_exc: Exception | None = None
    for attempt in range(1, s.retries + 2):
        audio = bytearray()
        cues: list[tuple[float, float, str]] = []
        try:
            comm = edge_tts.Communicate(
                text,
                job.voice,
                rate=job.rate,
                pitch=job.pitch,
                volume=job.volume,
                boundary=boundary,
                proxy=s.proxy,
                connect_timeout=s.connect_timeout,
                receive_timeout=s.receive_timeout,
            )
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                    start = chunk["offset"] / 1e7
                    end = (chunk["offset"] + chunk["duration"]) / 1e7
                    cues.append((offset + start, offset + end, chunk["text"]))
            if not audio:
                raise edge_tts.exceptions.NoAudioReceived("لم يتم استلام أي صوت")
            duration = cues[-1][1] - offset if cues else 0.0
            return bytes(audio), cues, duration
        except (edge_tts.exceptions.UnknownResponse, ValueError) as exc:
            # أخطاء في المدخلات (صوت غير موجود...) لا فائدة من إعادتها
            if isinstance(exc, ValueError):
                raise
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 - أخطاء الشبكة متنوعة
            last_exc = exc
        if attempt <= s.retries:
            delay = min(2 ** attempt, 30) + random.uniform(0, 1)
            log.warning("فشلت المحاولة %d (%s). إعادة بعد %.1f ث...", attempt, last_exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError(f"فشل التوليد بعد {s.retries + 1} محاولات: {last_exc}")


def write_srt(path: Path, cues: list[tuple[float, float, str]], word_mode: bool) -> None:
    # في وضع الكلمات نجمع كل ~8 كلمات في سطر واحد لسهولة القراءة
    if word_mode:
        grouped: list[tuple[float, float, str]] = []
        buf: list[tuple[float, float, str]] = []
        for cue in cues:
            buf.append(cue)
            if len(buf) >= 8 or cue[2].endswith((".", "!", "?", "؟", "،", ",")):
                grouped.append((buf[0][0], buf[-1][1], " ".join(c[2] for c in buf)))
                buf = []
        if buf:
            grouped.append((buf[0][0], buf[-1][1], " ".join(c[2] for c in buf)))
        cues = grouped
    lines = []
    for i, (start, end, text) in enumerate(cues, 1):
        lines.append(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


async def run_job(job: Job, s: Settings) -> Result:
    t0 = time.perf_counter()
    try:
        if job.output.exists() and not s.overwrite:
            log.info("تخطي (موجود مسبقاً): %s", job.output)
            return Result(job, True, 0.0, "skipped")
        chunks = split_text(job.text, s.max_chunk)
        if not chunks:
            raise ValueError("النص فارغ")
        job.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = job.output.with_name(job.output.name + ".part")
        all_cues: list[tuple[float, float, str]] = []
        offset = 0.0
        total = 0
        with open(tmp, "wb") as fh:
            for i, chunk in enumerate(chunks, 1):
                if len(chunks) > 1:
                    log.info("  [%s] جزء %d/%d (%d حرف)", job.output.name, i, len(chunks), len(chunk))
                data, cues, duration = await synthesize_chunk(chunk, job, s, offset)
                fh.write(data)
                total += len(data)
                all_cues.extend(cues)
                # MP3 بمعدل 48kbps => 6000 بايت/ثانية؛ تقدير احتياطي عند غياب التوقيتات
                offset += duration if duration > 0 else len(data) / 6000
        os.replace(tmp, job.output)
        srt_path = None
        if job.srt or job.word_srt:
            srt_path = job.output.with_suffix(".srt")
            write_srt(srt_path, all_cues, job.word_srt)
        return Result(job, True, time.perf_counter() - t0, bytes_written=total, srt_path=srt_path)
    except Exception as exc:  # noqa: BLE001
        tmp = job.output.with_name(job.output.name + ".part")
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return Result(job, False, time.perf_counter() - t0, error=str(exc))


async def run_jobs(jobs: list[Job], s: Settings, concurrency: int) -> list[Result]:
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def worker(job: Job) -> Result:
        nonlocal done
        async with sem:
            res = await run_job(job, s)
        done += 1
        status = "✔" if res.ok else "✘"
        detail = f"{res.bytes_written / 1024:.1f}KB في {res.seconds:.1f}ث" if res.ok else res.error
        log.info("[%d/%d] %s %s — %s", done, len(jobs), status, job.output, detail)
        return res

    return await asyncio.gather(*(worker(j) for j in jobs))


# --------------------------------------------------------------------------- #
# الدُفعات
# --------------------------------------------------------------------------- #
def load_batch(path: Path) -> list[dict]:
    """يقرأ ملف الدُفعات. الصيغ المدعومة:
    - .txt  : كل سطر غير فارغ = ملف صوتي واحد.
    - .csv  : أعمدة: text (إلزامي)، name, voice, rate, pitch, volume (اختيارية).
    - .json/.jsonl : قائمة كائنات بنفس الحقول، أو قائمة نصوص.
    """
    suffix = path.suffix.lower()
    raw = path.read_text(encoding="utf-8-sig")
    if suffix == ".csv":
        return [dict(r) for r in csv.DictReader(raw.splitlines())]
    if suffix == ".jsonl":
        items = [json.loads(line) for line in raw.splitlines() if line.strip()]
    elif suffix == ".json":
        items = json.loads(raw)
        if isinstance(items, dict):
            items = items.get("items", [items])
    else:
        items = [line for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return [{"text": it} if isinstance(it, str) else dict(it) for it in items]


# --------------------------------------------------------------------------- #
# الأصوات
# --------------------------------------------------------------------------- #
async def list_voices(lang: str | None, gender: str | None, proxy: str | None, as_json: bool) -> int:
    voices = await edge_tts.list_voices(proxy=proxy)
    if lang:
        voices = [v for v in voices if v["Locale"].lower().startswith(lang.lower())]
    if gender:
        voices = [v for v in voices if v["Gender"].lower() == gender.lower()]
    voices.sort(key=lambda v: v["ShortName"])
    if as_json:
        print(json.dumps(voices, ensure_ascii=False, indent=2))
        return 0
    print(f"{'Name':<42} {'Gender':<8} {'Locale':<8}")
    print("-" * 60)
    for v in voices:
        print(f"{v['ShortName']:<42} {v['Gender']:<8} {v['Locale']:<8}")
    print(f"\nالمجموع: {len(voices)} صوت")
    return 0


# --------------------------------------------------------------------------- #
# الواجهة
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tts.py",
        description="مولّد أصوات باستخدام Microsoft Edge TTS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("أمثلة:")[-1] if __doc__ else None,
    )
    src = p.add_argument_group("مصدر النص")
    src.add_argument("text", nargs="?", help="النص المراد تحويله إلى صوت")
    src.add_argument("-f", "--file", type=Path, help="ملف نصي يحتوي النص")
    src.add_argument("-b", "--batch", type=Path, help="ملف دُفعات (.txt/.csv/.json/.jsonl)")

    voice = p.add_argument_group("الصوت")
    voice.add_argument("-v", "--voice", help=f"اسم الصوت أو اختصار ({', '.join(VOICE_ALIASES)})")
    voice.add_argument("-r", "--rate", default="+0%", help="السرعة، مثل +20%% أو -10%%")
    voice.add_argument("-p", "--pitch", default="+0Hz", help="الطبقة، مثل +5Hz أو -10Hz")
    voice.add_argument("--volume", default="+0%", help="مستوى الصوت، مثل +50%% أو -20%%")

    out = p.add_argument_group("المخرجات")
    out.add_argument("-o", "--output", type=Path, help="ملف الإخراج (افتراضي: اسم مشتق من النص)")
    out.add_argument("-d", "--out-dir", type=Path, default=Path("output"), help="مجلد الإخراج (افتراضي: output)")
    out.add_argument("--srt", action="store_true", help="توليد ترجمة SRT على مستوى الجمل")
    out.add_argument("--word-srt", action="store_true", help="توليد ترجمة SRT بتوقيت الكلمات")
    out.add_argument("--no-overwrite", action="store_true", help="تخطي الملفات الموجودة مسبقاً")

    misc = p.add_argument_group("متقدم")
    misc.add_argument("-l", "--list-voices", action="store_true", help="عرض الأصوات المتاحة")
    misc.add_argument("--lang", help="تصفية الأصوات حسب اللغة (مثل ar أو ar-SA)")
    misc.add_argument("--gender", choices=["Male", "Female", "male", "female"], help="تصفية حسب الجنس")
    misc.add_argument("--json", action="store_true", help="عرض الأصوات بصيغة JSON")
    misc.add_argument("-j", "--jobs", type=int, default=3, help="عدد المهام المتوازية في وضع الدُفعات")
    misc.add_argument("--retries", type=int, default=4, help="عدد مرات إعادة المحاولة")
    misc.add_argument("--max-chunk", type=int, default=MAX_CHUNK_CHARS, help="أقصى طول للجزء الواحد")
    misc.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
                      help="بروكسي HTTP (افتراضي: متغير HTTPS_PROXY)")
    misc.add_argument("--ca-bundle", default=os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"),
                      help="ملف شهادات CA إضافي (افتراضي: SSL_CERT_FILE)")
    misc.add_argument("-q", "--quiet", action="store_true", help="إخفاء الرسائل غير الضرورية")
    misc.add_argument("--debug", action="store_true", help="رسائل تفصيلية")
    return p


def make_job(item: dict, args: argparse.Namespace, index: int | None) -> Job:
    text = str(item.get("text", "")).strip()
    voice = resolve_voice(item.get("voice") or args.voice, text)
    if args.output and index is None:
        output = args.output
    else:
        name = item.get("name") or item.get("output")
        if not name:
            prefix = f"{index:03d}_" if index is not None else ""
            name = prefix + safe_filename(text[:40])
        output = Path(name)
        if not output.suffix:
            output = output.with_suffix(".mp3")
        if not output.is_absolute():
            output = args.out_dir / output
    return Job(
        text=text,
        output=output,
        voice=voice,
        rate=normalize_signed(item.get("rate") or args.rate, "%", "rate"),
        pitch=normalize_signed(item.get("pitch") or args.pitch, "Hz", "pitch"),
        volume=normalize_signed(item.get("volume") or args.volume, "%", "volume"),
        srt=args.srt,
        word_srt=args.word_srt,
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else (logging.WARNING if args.quiet else logging.INFO),
        format="%(message)s",
    )
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    configure_ssl(args.ca_bundle)

    if args.list_voices:
        try:
            return asyncio.run(list_voices(args.lang, args.gender, args.proxy, args.json))
        except Exception as exc:  # noqa: BLE001
            log.error("تعذّر جلب قائمة الأصوات: %s", exc)
            return 1

    try:
        if args.batch:
            items = load_batch(args.batch)
            jobs = [make_job(it, args, i) for i, it in enumerate(items, 1) if str(it.get("text", "")).strip()]
        else:
            if args.text:
                text = args.text
            elif args.file:
                text = args.file.read_text(encoding="utf-8-sig")
            elif not sys.stdin.isatty():
                text = sys.stdin.read()
            else:
                parser.print_help()
                return 2
            if not text.strip():
                log.error("النص فارغ.")
                return 2
            item = {"text": text}
            if args.file and not args.output:
                item["name"] = args.file.stem
            jobs = [make_job(item, args, None)]
    except (argparse.ArgumentTypeError, OSError, json.JSONDecodeError) as exc:
        log.error("خطأ: %s", exc)
        return 2

    if not jobs:
        log.error("لا توجد نصوص لتوليدها.")
        return 2

    settings = Settings(
        retries=max(0, args.retries),
        proxy=args.proxy,
        max_chunk=max(200, args.max_chunk),
        overwrite=not args.no_overwrite,
    )
    log.info("بدء توليد %d ملف(ات)...", len(jobs))
    if len(jobs) == 1:
        log.info("الصوت: %s | السرعة: %s | الطبقة: %s | المستوى: %s",
                 jobs[0].voice, jobs[0].rate, jobs[0].pitch, jobs[0].volume)

    try:
        results = asyncio.run(run_jobs(jobs, settings, args.jobs))
    except KeyboardInterrupt:
        log.error("تم الإلغاء.")
        return 130

    failed = [r for r in results if not r.ok]
    for r in results:
        if r.ok and r.srt_path:
            log.info("الترجمة: %s", r.srt_path)
    log.info("\nتم: %d ناجح، %d فاشل.", len(results) - len(failed), len(failed))
    for r in failed:
        log.error("  ✘ %s: %s", r.job.output, r.error)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
