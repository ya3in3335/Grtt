#!/usr/bin/env python3
"""
promo_voiceover.py - يضيف تعليقاً صوتياً (Edge TTS) وموسيقى خلفية إلى فيديو إعلاني.

المدخلات: فيديو + ملف سيناريو JSON يصف المقاطع الصوتية وتوقيتها ونبرتها.
المخرجات: فيديو MP4 جاهز (الصورة كما هي دون إعادة ترميز) + ملف ترجمة SRT للتعليق.

ما يقوم به:
  1. يولّد كل جملة بالصوت والنبرة المطلوبين (rate / pitch / volume لكل مقطع).
  2. يقصّ الصمت الزائد ويقيس المدة؛ وإذا تجاوزت الجملة مدة مشهدها يسرّعها تلقائياً حتى تتسع.
  3. يولّد موسيقى خلفية مضبوطة على الإيقاع والمشاهد (music_gen.py) أو يستعمل ملف موسيقى جاهزاً.
  4. يعالج الصوت (تنقية، ضغط، وضوح) ويخفض الموسيقى تلقائياً أثناء الكلام (ducking).
  5. يضبط علوّ الصوت النهائي على معيار المنصات (-14 LUFS) ويدمج كل شيء في الفيديو.

مثال:
  python promo_voiceover.py projects/sodimax/script.json -i promo.mp4 -o promo_final.mp4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import music_gen
import tts

log = logging.getLogger("promo")


@dataclass
class Segment:
    start: float
    end: float
    text: str
    voice: str
    rate: int
    pitch: str
    volume: str
    wav: Path | None = None
    duration: float = 0.0


def run(cmd: list[str]) -> str:
    log.debug("$ %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"فشل الأمر: {' '.join(cmd[:3])}...\n{res.stderr[-2000:]}")
    return res.stdout + res.stderr


def probe_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip().splitlines()[0])


def has_audio(path: Path) -> bool:
    out = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(out.strip())


def pct(v) -> int:
    return int(str(v).replace("%", "").replace("+", "") or 0)


async def synth_segment(seg: Segment, work: Path, idx: int, settings: tts.Settings,
                        max_rate: int, max_tempo: float) -> None:
    """يولّد المقطع ويكيّف سرعته ليتسع في المدة المتاحة."""
    slot = seg.end - seg.start
    rate = seg.rate
    while True:
        mp3 = work / f"seg{idx:02d}.mp3"
        job = tts.Job(text=seg.text, output=mp3, voice=seg.voice, rate=f"{rate:+d}%",
                      pitch=seg.pitch, volume=seg.volume)
        res = await tts.run_job(job, settings)
        if not res.ok:
            raise RuntimeError(f"فشل توليد المقطع {idx}: {res.error}")
        wav = work / f"seg{idx:02d}.wav"
        # قص الصمت من البداية والنهاية
        trim = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.02,"
                "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05,areverse")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3), "-af", trim, "-ar", "44100", "-ac", "1", str(wav)])
        dur = probe_duration(wav)
        if dur <= slot or rate >= max_rate:
            break
        rate = min(max_rate, rate + max(4, int((dur / slot - 1) * 100) + 2))
        log.info("  المقطع %d أطول من مشهده (%.2fث > %.2fث) ← إعادة بسرعة %+d%%", idx, dur, slot, rate)

    if dur > slot:
        tempo = min(max_tempo, dur / slot)
        log.warning("  المقطع %d ما زال طويلاً؛ تسريع إضافي ×%.2f", idx, tempo)
        fitted = work / f"seg{idx:02d}_fit.wav"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-af", f"atempo={tempo:.4f}", str(fitted)])
        wav, dur = fitted, probe_duration(fitted)
        if dur > slot + 0.05:
            log.warning("  ⚠ المقطع %d يتجاوز مشهده بـ %.2fث — اختصر النص.", idx, dur - slot)
    seg.wav, seg.duration, seg.rate = wav, dur, rate
    log.info("  ✔ [%5.2f → %5.2f] %.2fث (سرعة %+d%%) %s", seg.start, seg.start + dur, dur, rate, seg.text)


def write_srt(segments: list[Segment], path: Path) -> None:
    lines = []
    for i, s in enumerate(segments, 1):
        lines.append(f"{i}\n{tts.srt_time(s.start)} --> {tts.srt_time(s.start + s.duration)}\n{s.text}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def build_music(cfg: dict, duration: float, work: Path, base: Path) -> Path:
    m = cfg.get("music", {}) or {}
    if m.get("file"):
        src = Path(m["file"])
        if not src.is_absolute():
            src = base / src
        out = work / "music.wav"
        fade = min(2.0, duration / 4)
        run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(src), "-t", f"{duration:.3f}",
             "-af", f"afade=t=out:st={duration - fade:.3f}:d={fade:.3f}", "-ar", "44100", "-ac", "2", str(out)])
        return out
    audio = music_gen.generate(
        duration,
        bpm=m.get("bpm", 100),
        drop=m.get("drop", 4.0),
        outro=m.get("outro"),
        transitions=m.get("transitions", []),
        mood=m.get("mood", "energetic"),
        seed=m.get("seed", 7),
        transpose=m.get("transpose", 0),
    )
    out = work / "music.wav"
    music_gen.write_wav(str(out), audio)
    return out


def mix(video: Path, segments: list[Segment], music: Path, cfg: dict, out: Path, duration: float,
        keep_original: bool) -> None:
    m = cfg.get("music", {}) or {}
    music_db = m.get("volume_db", -10)
    duck = m.get("duck", 0.6)  # 0..1 مقدار خفض الموسيقى أثناء الكلام
    orig_db = cfg.get("original_audio_db", -12)

    inputs = ["-i", str(video), "-i", str(music)]
    for s in segments:
        inputs += ["-i", str(s.wav)]

    f = []
    vlabels = []
    for i, s in enumerate(segments):
        d = int(round(s.start * 1000))
        f.append(f"[{i + 2}:a]aformat=sample_rates=44100:channel_layouts=mono,adelay={d}:all=1[v{i}]")
        vlabels.append(f"[v{i}]")
    # سلسلة معالجة الصوت البشري
    f.append(
        "".join(vlabels) + f"amix=inputs={len(vlabels)}:normalize=0:dropout_transition=0,"
        "highpass=f=80,equalizer=f=250:t=q:w=1.2:g=-2,equalizer=f=3200:t=q:w=1.0:g=3.5,"
        "equalizer=f=9000:t=h:w=1:g=2,"
        "acompressor=threshold=-20dB:ratio=3.5:attack=5:release=120:makeup=4,"
        "aformat=channel_layouts=stereo,"
        f"apad=whole_dur={duration:.3f},asplit=2[voice][key]"
    )
    music_chain = f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={music_db}dB"
    if keep_original and has_audio(video):
        f.append(f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={orig_db}dB[orig]")
        f.append(f"{music_chain}[mus0]")
        f.append("[mus0][orig]amix=inputs=2:normalize=0[bed]")
    else:
        f.append(f"{music_chain}[bed]")
    ratio = 1 + duck * 9
    f.append(f"[bed][key]sidechaincompress=threshold=0.02:ratio={ratio:.1f}:attack=15:release=350:makeup=1[ducked]")
    f.append("[ducked][voice]amix=inputs=2:normalize=0,"
             f"loudnorm=I={cfg.get('loudness', -14)}:TP=-1.5:LRA=11,"
             f"atrim=0:{duration:.3f},afade=t=out:st={max(0, duration - 0.4):.3f}:d=0.4[aout]")

    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(f),
           "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-ar", "48000", "-movflags", "+faststart", "-t", f"{duration:.3f}", str(out)]
    run(cmd)


def load_segments(cfg: dict, duration: float) -> list[Segment]:
    voice = tts.resolve_voice(cfg.get("voice"), " ".join(s["text"] for s in cfg["segments"]))
    segs = []
    raw = sorted(cfg["segments"], key=lambda s: s["start"])
    for i, s in enumerate(raw):
        end = s.get("end") or (raw[i + 1]["start"] - 0.15 if i + 1 < len(raw) else duration - 0.2)
        segs.append(Segment(
            start=float(s["start"]),
            end=float(min(end, duration)),
            text=s["text"].strip(),
            voice=tts.resolve_voice(s.get("voice") or voice, s["text"]),
            rate=pct(s.get("rate", cfg.get("rate", "+0%"))),
            pitch=tts.normalize_signed(s.get("pitch", cfg.get("pitch", "+0Hz")), "Hz", "pitch"),
            volume=tts.normalize_signed(s.get("volume", cfg.get("volume", "+0%")), "%", "volume"),
        ))
    return segs


async def synth_all(segs, work, settings, max_rate, max_tempo):
    # توازي محدود لتجنّب الحظر من الخادم
    sem = asyncio.Semaphore(3)

    async def one(i, s):
        async with sem:
            await synth_segment(s, work, i, settings, max_rate, max_tempo)

    await asyncio.gather(*(one(i, s) for i, s in enumerate(segs)))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="إضافة تعليق صوتي وموسيقى لفيديو إعلاني")
    p.add_argument("script", type=Path, help="ملف السيناريو JSON")
    p.add_argument("-i", "--input", type=Path, help="الفيديو (يتجاوز قيمة video في السيناريو)")
    p.add_argument("-o", "--output", type=Path, help="الفيديو الناتج")
    p.add_argument("--keep-original", action="store_true", help="إبقاء صوت الفيديو الأصلي تحت الموسيقى")
    p.add_argument("--voice-only", action="store_true", help="بدون موسيقى مولّدة (مع --keep-original يبقي الأصلية)")
    p.add_argument("--max-rate", type=int, default=35, help="أقصى تسريع للكلام عبر edge-tts (%%)")
    p.add_argument("--max-tempo", type=float, default=1.15, help="أقصى تسريع إضافي بعد التوليد")
    p.add_argument("--work-dir", type=Path, help="مجلد الملفات الوسيطة (افتراضي: مؤقت)")
    p.add_argument("--debug", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO, format="%(message)s")
    logging.getLogger("tts").setLevel(logging.WARNING)

    cfg = json.loads(a.script.read_text(encoding="utf-8"))
    base = a.script.parent
    video = a.input or (base / cfg["video"] if cfg.get("video") else None)
    if not video or not video.exists():
        log.error("الفيديو غير موجود: %s", video)
        return 2
    out = a.output or video.with_name(video.stem + "_voiced.mp4")
    duration = probe_duration(video)

    tts.configure_ssl(os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"))
    settings = tts.Settings(proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))

    work_ctx = tempfile.TemporaryDirectory() if not a.work_dir else None
    work = a.work_dir or Path(work_ctx.name)
    work.mkdir(parents=True, exist_ok=True)
    try:
        segs = load_segments(cfg, duration)
        log.info("🎙  توليد %d مقطع صوتي (%s)...", len(segs), segs[0].voice)
        asyncio.run(synth_all(segs, work, settings, a.max_rate, a.max_tempo))

        if a.voice_only:
            log.info("🎵 بدون موسيقى مولّدة")
            silent = work / "silence.wav"
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-t", f"{duration:.3f}", str(silent)])
            music = silent
        else:
            log.info("🎵 توليد الموسيقى...")
            music = build_music(cfg, duration, work, base)

        log.info("🎚  المزج والدمج في الفيديو...")
        mix(video, segs, music, cfg, out, duration, a.keep_original)
        srt = out.with_suffix(".srt")
        write_srt(segs, srt)
        log.info("\n✅ تم: %s\n   الترجمة: %s", out, srt)
    finally:
        if work_ctx:
            work_ctx.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
