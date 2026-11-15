# النشر — Sticker Studio 3.0.0

البوت يعمل بـ **long polling**: لا يحتاج webhook ولا منفذًا مفتوحًا ولا جلسة تيليجرام شخصية. أي استضافة تسمح بعملية Python دائمة واتصال إنترنت صادر تكفي.

## 1) تجهيز الاستضافة

تأكد من:

```bash
python --version     # 3.11 أو أحدث
ffmpeg -version      # مطلوب لتحويل GIF/الفيديو/الصوت
ffprobe -version
```

إذا لم يتوفر FFmpeg: التحويل إلى WEBP فقط يعمل، أما WEBM و`/motion` فسيعطيان رسالة واضحة.
إذا ثبّت FFmpeg يدويًا في مجلد داخل المشروع، اضبط `FFMPEG_PATH` و`FFPROBE_PATH` بمساريه الكاملين بدل الاعتماد على `PATH`.

ملاحظة: الملصقات الواردة من تيليجرام (WEBP/TGS/WEBM) تُحفظ كما هي ولا تحتاج FFmpeg.

## 2) رفع المشروع

ارفع محتويات المشروع بحيث يكون `bot/` و`requirements.txt` في جذر مجلد التشغيل. لا ترفع `.env` أو `.venv` أو `data/`.

## 3) تثبيت المتطلبات

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# اختياري:
python -m pip install -r requirements-pdf.txt -r requirements-vector.txt
# اختياري وثقيل:
python -m pip install -r requirements-ai.txt
```

## 4) الإعداد

انسخ `.env.example` إلى `.env` واضبط:

```env
BOT_TOKEN=توكن_البوت
ADMIN_IDS=رقم_تيليجرام_الخاص_بك
```

للحصول على رقمك: شغّل البوت مؤقتًا وأرسل `/id`، أو استخدم أي بوت معرفة ID. أضف الرقم في `ADMIN_IDS` ثم أعد التشغيل.

يفضل استخدام **Environment Variables** في لوحة الاستضافة بدل ملف `.env` إن كانت اللوحة تدعم ذلك.

## 5) أمر التشغيل

```bash
python -m bot.main
```

على Wispbyte أو أي لوحة مشابهة: ضع الأمر أعلاه في **Startup Command**، وتأكد أن بيضة Python تحتوي FFmpeg.

## 6) التحقق

في السجلات يجب أن تظهر رسالة:

```
Sticker Studio started as @your_bot
```

ثم في تيليجرام:

1. `/start`
2. أرسل صورة عادية كـ Photo ⇒ يعود WEBP كـ Document.
3. `/newpack` ← اسم الحزمة ← أرسل صورة ⇒ يعود رابط `t.me/addstickers/...`.

## 7) Docker

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f
```

الحجم `sticker_data` يحفظ SQLite والملفات. البوت ينسخ قاعدة البيانات تلقائيًا كل 6 ساعات إلى `/app/data/backups` (قابل للضبط بـ `BACKUP_INTERVAL_HOURS` و`BACKUP_KEEP`). ويمكن أخذ نسخة كاملة من الحجم عند الحاجة:

```bash
docker run --rm -v sticker-studio_sticker_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/sticker-data-backup.tar.gz -C /data .
```

## 8) systemd

```ini
[Unit]
Description=Sticker Studio Bot
After=network.target

[Service]
WorkingDirectory=/opt/sticker-studio
EnvironmentFile=/opt/sticker-studio/.env
ExecStart=/opt/sticker-studio/.venv/bin/python -m bot.main
Restart=always
RestartSec=5
User=sticker

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sticker-studio
```

## 9) أخطاء شائعة

| الخطأ | السبب | الحل |
|---|---|---|
| `BOT_TOKEN is missing` | لا يوجد `.env` أو التوكن فارغ | اضبط `BOT_TOKEN` |
| `BOT_TOKEN was rejected` | توكن خاطئ/ملغى | أنشئ توكنًا جديدًا من BotFather |
| `Conflict: terminated by other getUpdates` | نسختان تعملان بنفس التوكن | أوقف إحداهما |
| `FFmpeg غير مثبت` | لا يوجد ffmpeg في PATH | ثبّته أو اضبط `FFMPEG_PATH` |
| `TimedOut` عند البدء | شبكة الاستضافة متعطلة مؤقتًا | اتركه يعيد المحاولة تلقائيًا |
| الأوامر تعمل لكن رفع الملفات يتجمد | الشبكة تحجب حركة ملفات تيليجرام | استخدم استضافة أخرى أو `TELEGRAM_PROXY` |
| تعذر إنشاء الحزمة | لم يبدأ المستخدم محادثة مع البوت | أرسل `/start` أولًا |
| لا يضيف الملصق للحزمة | صيغة مختلفة عن الحزمة | `/endpack` ثم حزمة جديدة بنفس الصيغة |

## 10) ملاحظات مهمة

- **لا تشغّل نسختين بنفس التوكن** في الوقت نفسه.
- لا تشارك `.env` أو التوكن. يمكنك إبطال التوكن من `/revoke` في BotFather.
- إذا كان الملف أكبر من 20 MB فلن يستطيع البوت تنزيله عبر Bot API العادي.
- حدّث `ADMIN_IDS` بعد معرفة رقمك، وإلا لن تعمل أوامر `/admin` و`/broadcast`.
