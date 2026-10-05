# مولّد الأصوات بتقنية Edge TTS

سكريبت بايثون (`tts.py`) يحوّل النص إلى صوت باستخدام أصوات Microsoft Edge العصبية (مجاناً، دون مفتاح API).

## التثبيت

```bash
pip install -r requirements.txt
```

## الاستخدام

```bash
# نص مباشر (يختار صوتاً عربياً تلقائياً عند وجود نص عربي)
python tts.py "مرحبا بك في عالم الذكاء الاصطناعي" -o hello.mp3

# من ملف، مع صوت وسرعة محددين وملف ترجمة SRT
python tts.py -f article.txt -v ar-EG-SalmaNeural --rate +10% --srt

# من stdin
echo "Hello world" | python tts.py -o hello.mp3

# عرض الأصوات العربية (أو النسائية فقط)
python tts.py --list-voices --lang ar
python tts.py --list-voices --lang ar --gender Female

# وضع الدُفعات: عدة ملفات بالتوازي
python tts.py --batch examples/batch.csv --out-dir voices/ --jobs 4
python tts.py --batch examples/lines.txt --srt
```

## الخيارات المهمة

| الخيار | الوصف |
|---|---|
| `-v, --voice` | اسم الصوت الكامل أو اختصار: `hamed`, `zariyah`, `salma`, `shakir`, `fatima`, `hamdan`, `amina`, `ismael`, `jamal`, `mouna`, `andrew`, `emma`, `ava`, `brian` |
| `-r, --rate` | السرعة: `+20%`، `-10%` |
| `-p, --pitch` | الطبقة: `+5Hz`، `-10Hz` |
| `--volume` | مستوى الصوت: `+50%`، `-20%` |
| `-o, --output` | ملف الإخراج (افتراضياً داخل مجلد `output/`) |
| `--srt` / `--word-srt` | توليد ترجمة على مستوى الجمل / الكلمات |
| `-b, --batch` | ملف دُفعات `.txt` أو `.csv` أو `.json` أو `.jsonl` |
| `-j, --jobs` | عدد المهام المتوازية (افتراضي 3) |
| `--retries` | عدد إعادة المحاولة عند أخطاء الشبكة (افتراضي 4) |
| `--no-overwrite` | تخطي الملفات الموجودة (مفيد لاستكمال دُفعة متوقفة) |
| `--proxy` / `--ca-bundle` | بروكسي وشهادات CA مخصصة (تُقرأ تلقائياً من `HTTPS_PROXY` و`SSL_CERT_FILE`) |

## صيغة ملفات الدُفعات

- **TXT**: كل سطر = ملف صوتي.
- **CSV**: عمود `text` إلزامي، و`name`, `voice`, `rate`, `pitch`, `volume` اختيارية.
- **JSON/JSONL**: قائمة كائنات بنفس الحقول، أو قائمة نصوص.

## المزايا

- تقسيم النصوص الطويلة تلقائياً عند حدود الجمل ودمجها في ملف واحد بتوقيتات ترجمة متصلة.
- إعادة المحاولة مع تراجع أسّي، وكتابة ذرّية للملفات (لا ملفات ناقصة عند الفشل).
- رموز خروج واضحة: `0` نجاح، `1` فشل توليد، `2` خطأ في المدخلات.

---

# 🎬 إضافة تعليق صوتي وموسيقى لفيديو إعلاني

```bash
python promo_voiceover.py projects/sodimax/script.json -i promo.mp4 -o promo_final.mp4
```

يولّد الكلام بالنبرة المطلوبة لكل مشهد، ويسرّع الجمل تلقائياً لتتسع في مشهدها، ويولّد موسيقى خلفية بدون حقوق
(`music_gen.py`) مضبوطة على الإيقاع والانتقالات، ويخفض الموسيقى أثناء الكلام، ثم يضبط العلوّ على -14 LUFS
ويدمج الصوت في الفيديو دون إعادة ترميز الصورة. ينتج أيضاً ملف `.srt` للتعليق.

خيارات: `--keep-original` (إبقاء صوت الفيديو الأصلي تحت الموسيقى)، `--voice-only`، `--work-dir`.

## صيغة السيناريو (JSON)

```json
{
  "voice": "ar-DZ-IsmaelNeural", "rate": "+6%", "pitch": "+3Hz",
  "music": {"mood": "energetic", "bpm": 100, "drop": 4, "outro": 28,
            "transitions": [9, 14, 18, 23], "volume_db": -9, "duck": 0.6},
  "segments": [
    {"start": 4.4, "end": 8.8, "text": "كُلّ الخُرْدَوات... وَلّات في جيبك!", "pitch": "+4Hz"}
  ]
}
```

- `music.mood`: `energetic` (حماسي) / `elegant` (فاخر) / `happy` (مرح). أو `music.file` لاستعمال ملف موسيقى جاهز.
- كل مقطع يقبل `voice`, `rate`, `pitch`, `volume` خاصة به (لصنع النبرة: الحماس = سرعة وطبقة أعلى، الدعوة للفعل = أبطأ وأعلى صوتاً).
- التشكيل (الحركات) يحسّن نطق الكلمات الصعبة.

## موسيقى فقط

```bash
python music_gen.py -o music.wav --duration 30 --bpm 100 --drop 4 --outro 26 --transitions 8,14,20 --mood energetic
```
