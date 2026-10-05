#!/usr/bin/env python3
"""
music_gen.py - مولّد موسيقى خلفية إعلانية (بدون حقوق) مُركَّبة برمجياً.

يولّد مقطعاً موسيقياً بإيقاع إلكتروني حديث مضبوطاً على توقيت الفيديو:
  * مقدّمة (intro) هادئة مع ضربة للشعار وصعود (riser) قبل الانطلاق.
  * انطلاق (drop) بإيقاع كامل: كيك، تصفيق، هاي-هات، باص، وسادة كوردات، أربيجيو.
  * مؤثرات انتقال (whoosh) عند تغيّر المشاهد.
  * خاتمة (outro) بضربة نهائية وذيل هادئ مع تلاشٍ.

مثال:
  python music_gen.py -o music.wav --duration 32 --bpm 100 --drop 4 --outro 28 \
      --transitions 9,14,18,23 --mood energetic
"""

from __future__ import annotations

import argparse
import sys
import wave

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

SR = 44100

# المقامات: درجات الكوردات (أرقام MIDI للجذر) وأنواعها
MOODS = {
    # حماسي: i - VI - III - VII  (Am F C G)
    "energetic": {"chords": [(45, "min"), (41, "maj"), (48, "maj"), (43, "maj")], "swing": 0.0},
    # فاخر/هادئ: i - iv - VI - V  (Am Dm F E)
    "elegant": {"chords": [(45, "min"), (50, "min"), (41, "maj"), (40, "maj")], "swing": 0.0},
    # مرح: I - V - vi - IV (C G Am F)
    "happy": {"chords": [(48, "maj"), (43, "maj"), (45, "min"), (41, "maj")], "swing": 0.0},
}
INTERVALS = {"maj": (0, 4, 7), "min": (0, 3, 7)}


def midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def lp(x: np.ndarray, cutoff: float, order: int = 2) -> np.ndarray:
    sos = butter(order, min(cutoff, SR / 2 - 100), "low", fs=SR, output="sos")
    return sosfilt(sos, x, axis=0)


def hp(x: np.ndarray, cutoff: float, order: int = 2) -> np.ndarray:
    sos = butter(order, cutoff, "high", fs=SR, output="sos")
    return sosfilt(sos, x, axis=0)


def bp(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    sos = butter(2, [lo, hi], "band", fs=SR, output="sos")
    return sosfilt(sos, x, axis=0)


def saw(freq: float, n: int, phase: float = 0.0) -> np.ndarray:
    t = np.arange(n) / SR
    return 2.0 * ((t * freq + phase) % 1.0) - 1.0


class Track:
    """مسار ستيريو بسيط نضيف إليه الأصوات في مواضع زمنية."""

    def __init__(self, seconds: float):
        self.buf = np.zeros((int(seconds * SR) + SR, 2))

    def add(self, sig: np.ndarray, at: float, gain: float = 1.0, pan: float = 0.0) -> None:
        i = int(at * SR)
        if i >= len(self.buf) or i < 0:
            return
        if sig.ndim == 1:
            l, r = np.cos((pan + 1) * np.pi / 4), np.sin((pan + 1) * np.pi / 4)
            sig = np.stack([sig * l * 1.414, sig * r * 1.414], axis=1)
        n = min(len(sig), len(self.buf) - i)
        self.buf[i : i + n] += sig[:n] * gain


# ---------------------------------------------------------------- instruments
def kick(punch: float = 1.0) -> np.ndarray:
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    f = 45 + 110 * np.exp(-t * 28)
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t * 6.5)
    click = hp(np.random.randn(n), 2000) * np.exp(-t * 250) * 0.3
    return np.tanh((body + click) * 1.6 * punch) * 0.9


def clap() -> np.ndarray:
    n = int(0.3 * SR)
    t = np.arange(n) / SR
    env = np.zeros(n)
    for d in (0.0, 0.011, 0.022):
        k = int(d * SR)
        env[k:] += np.exp(-(t[: n - k]) * (60 if d < 0.02 else 18))
    return bp(np.random.randn(n), 900, 5000) * env * 0.55


def hat(open_: bool = False) -> np.ndarray:
    n = int((0.25 if open_ else 0.05) * SR)
    t = np.arange(n) / SR
    return hp(np.random.randn(n), 7000, 4) * np.exp(-t * (14 if open_ else 90)) * 0.35


def impact(length: float = 2.5) -> np.ndarray:
    n = int(length * SR)
    t = np.arange(n) / SR
    f = 30 + 90 * np.exp(-t * 6)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 2.2)
    noise = lp(np.random.randn(n), 3000) * np.exp(-t * 5) * 0.4
    return np.tanh((boom + noise) * 1.3) * 0.9


def riser(length: float) -> np.ndarray:
    n = int(length * SR)
    t = np.linspace(0, 1, n)
    noise = np.random.randn(n)
    # مرشّح متدرّج: نصنع عدة نطاقات ونمزجها حسب الزمن
    out = np.zeros(n)
    bands = [400, 1200, 3000, 7000]
    for i, c in enumerate(bands):
        w = np.clip(1 - np.abs(t * (len(bands) - 1) - i), 0, 1)
        out += bp(noise, c * 0.6, c * 1.6) * w
    return out * (t ** 2) * 0.5


def whoosh(length: float = 0.9) -> np.ndarray:
    n = int(length * SR)
    t = np.linspace(0, 1, n)
    env = np.sin(np.pi * t) ** 2
    lo = bp(np.random.randn(n), 500, 2500)
    hi = bp(np.random.randn(n), 2500, 8000)
    return (lo * (1 - t) + hi * t) * env * 0.35


def pad_chord(notes: list[int], length: float, cutoff: float = 1800) -> np.ndarray:
    n = int(length * SR)
    out = np.zeros((n, 2))
    for m in notes:
        f = midi_hz(m)
        for det, pan in ((-0.12, -0.7), (0.0, 0.0), (0.12, 0.7)):
            s = saw(f * 2 ** (det / 12), n, np.random.rand())
            out[:, 0] += s * (1 - pan) / 2
            out[:, 1] += s * (1 + pan) / 2
    out = lp(out, cutoff, 2)
    t = np.arange(n) / SR
    env = np.minimum(1, t / 0.25) * np.minimum(1, (length - t) / 0.3).clip(0, 1)
    return out * env[:, None] * 0.06


def pluck(m: int, length: float = 0.35) -> np.ndarray:
    n = int(length * SR)
    t = np.arange(n) / SR
    f = midi_hz(m)
    s = saw(f, n) * 0.5 + np.sin(2 * np.pi * f * t)
    return lp(s, 2500) * np.exp(-t * 11) * 0.22


def bass_note(m: int, length: float) -> np.ndarray:
    n = int(length * SR)
    t = np.arange(n) / SR
    f = midi_hz(m)
    s = saw(f, n) * 0.6 + np.sin(2 * np.pi * f * t) * 0.8
    s = lp(s, 380, 2)
    env = np.minimum(1, t / 0.005) * np.exp(-t * 3)
    return np.tanh(s * env * 1.5) * 0.4


def reverb(x: np.ndarray, seconds: float = 1.8, mix: float = 0.25) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    ir = np.random.randn(n, 2) * np.exp(-t * 6.9 / seconds)[:, None]
    ir = lp(ir, 5000)
    ir /= np.sqrt((ir ** 2).sum(axis=0))
    wet = np.stack([fftconvolve(x[:, c], ir[:, c])[: len(x)] for c in range(2)], axis=1)
    return x * (1 - mix) + wet * mix


# ---------------------------------------------------------------- composition
def generate(
    duration: float,
    bpm: float = 100,
    drop: float = 4.0,
    outro: float | None = None,
    transitions: list[float] | None = None,
    mood: str = "energetic",
    seed: int = 7,
    transpose: int = 0,
) -> np.ndarray:
    np.random.seed(seed)
    outro = duration - 4 if outro is None else outro
    beat = 60.0 / bpm
    bar = beat * 4
    chords = [(r + transpose, q) for r, q in MOODS[mood]["chords"]]
    drums, music, fx = Track(duration), Track(duration), Track(duration)

    k, c, hc, ho = kick(), clap(), hat(), hat(True)

    # --- intro: وسادة هادئة + ضربة الشعار + صاعد قبل الانطلاق
    root, q = chords[0]
    notes = [root + 12 + i for i in INTERVALS[q]]
    if drop > 0.5:
        music.add(pad_chord(notes, drop + 0.3, cutoff=1100), 0.0, 2.2)
        for i, m in enumerate([notes[0] + 12, notes[2], notes[1], notes[0]]):
            music.add(pluck(m, 0.9), 0.3 + i * beat, 0.5, pan=0.3 if i % 2 else -0.3)
        fx.add(impact(min(3.0, drop + 1)), 0.25, 0.55)
        if drop > 1.5:
            fx.add(riser(min(2.2, drop - 0.3)), drop - min(2.2, drop - 0.3), 0.6)

    # --- main section (drop -> outro)
    fx.add(impact(2.0), drop, 0.45)
    t = drop
    bar_i = 0
    while t < outro - 1e-3:
        root, q = chords[bar_i % len(chords)]
        tri = [root + 12 + i for i in INTERVALS[q]]
        bar_len = min(bar, outro - t)
        music.add(pad_chord(tri, bar_len + 0.2), t, 1.0)
        for b in range(4):
            bt = t + b * beat
            if bt >= outro - 0.01:
                break
            drums.add(k, bt, 1.0)
            if b in (1, 3):
                drums.add(c, bt, 0.9, pan=0.05)
            for e in range(2):
                et = bt + e * beat / 2
                drums.add(ho if e == 1 and b == 3 else hc, et, 1.4 if e else 0.8, pan=0.3)
                # باص على الثُمنيات مع "سايدتشين" ناعم بعد الكيك
                bnote = root - 12 + (12 if (e == 1 and b % 2 == 1) else 0)
                music.add(bass_note(bnote, beat / 2), et + 0.03, 0.8 if e else 0.55)
            # أربيجيو
            arp = [tri[0] + 12, tri[1] + 12, tri[2] + 12, tri[1] + 24]
            for s in range(2):
                st = bt + s * beat / 2
                music.add(pluck(arp[(b * 2 + s) % 4]), st, 0.8, pan=-0.4 if s else 0.4)
        t += bar
        bar_i += 1

    # --- transitions
    for tr in transitions or []:
        if 0.6 < tr < duration:
            fx.add(whoosh(0.9), tr - 0.6, 0.7)

    # --- outro: ضربة نهائية وذيل الكورد الأول
    root, q = chords[0]
    tri = [root + 12 + i for i in INTERVALS[q]]
    fx.add(impact(3.0), outro, 0.7)
    tail = duration - outro + 0.5
    music.add(pad_chord(tri, tail, cutoff=1400), outro, 1.2)
    for i, m in enumerate([tri[0] + 24, tri[2] + 12, tri[1] + 12, tri[0] + 12]):
        music.add(pluck(m, 0.8), outro + i * beat, 0.7)

    # --- sidechain للموسيقى على الكيك (يعطي إحساس "النبض")
    duck = np.ones(len(music.buf))
    t = drop
    n_beat = int(beat * SR)
    curve = 1 - 0.55 * np.exp(-np.arange(n_beat) / SR * 9)
    while t < outro - 1e-3:
        i = int(t * SR)
        j = min(i + n_beat, len(duck))
        duck[i:j] = curve[: j - i]
        t += beat

    mix = drums.buf * 0.55 + reverb(music.buf * duck[:, None], 1.6, 0.22) * 1.5 + reverb(fx.buf, 2.2, 0.3) * 0.8
    mix = hp(mix, 30)
    mix = mix[: int(duration * SR)]
    # تلاشٍ في النهاية
    fade = int(min(1.5, duration - outro) * SR)
    if fade > 0:
        mix[-fade:] *= np.linspace(1, 0, fade)[:, None] ** 1.5
    peak = np.max(np.abs(mix)) or 1.0
    return np.tanh(mix / peak * 1.2) * 0.89


def write_wav(path: str, audio: np.ndarray) -> None:
    data = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


def parse_list(s: str | None) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()] if s else []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="مولّد موسيقى خلفية للإعلانات")
    p.add_argument("-o", "--output", default="music.wav")
    p.add_argument("--duration", type=float, required=True, help="المدة بالثواني")
    p.add_argument("--bpm", type=float, default=100)
    p.add_argument("--drop", type=float, default=4.0, help="لحظة انطلاق الإيقاع الكامل")
    p.add_argument("--outro", type=float, help="بداية الخاتمة (افتراضي: المدة - 4)")
    p.add_argument("--transitions", help="أزمنة الانتقالات مفصولة بفواصل: 9,14,18")
    p.add_argument("--mood", choices=list(MOODS), default="energetic")
    p.add_argument("--transpose", type=int, default=0, help="نقل المقام بأنصاف درجات")
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args(argv)
    audio = generate(a.duration, a.bpm, a.drop, a.outro, parse_list(a.transitions), a.mood, a.seed, a.transpose)
    write_wav(a.output, audio)
    print(f"✔ {a.output} ({a.duration:.1f}s, {a.bpm:g} BPM, {a.mood})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
