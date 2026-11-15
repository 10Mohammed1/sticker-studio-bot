# نشر Sticker Studio 4.1 على Railway

## طريقة النشر

المشروع يعمل كـ Telegram Long Polling Worker؛ لا يحتاج Domain أو Port أو Webhook.

1. ارفع محتويات هذا المجلد إلى مستودع GitHub.
2. في Railway أنشئ Project ثم Service من مستودع GitHub.
3. اترك Root Directory على `/`؛ يوجد `Dockerfile` في جذر المستودع وسيكتشفه Railway تلقائيًا.
4. أنشئ Volume للخدمة واضبط Mount Path على `/app/data`.
5. أضف المتغيرات أدناه في Variables.
6. تأكد أن الخدمة لها Replica واحدة فقط عند استخدام Telegram Long Polling مع SQLite.
7. Deploy ثم راقب Deploy Logs. ظهور `Sticker Studio started as @...` يعني أن اتصال البوت بدأ.

لا يوجد `railway.json` في الحزمة؛ توثيق Railway الحالي يعلن إهمال Config as Code، ويفضّل Infrastructure as Code للخدمات الجديدة. وجود Dockerfile في جذر المستودع يكفي لاكتشاف البناء تلقائيًا.

## المتغيرات المطلوبة

المطلوب فعليًا متغيّران فقط:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | توكن من @BotFather |
| `ADMIN_IDS` | رقم Telegram ID الخاص بك |

القيم التالية مضبوطة داخل الصورة أو افتراضية، وأضفها فقط إن أردت تغييرها:

| Variable | الافتراضي |
|---|---|
| `DATA_DIR` | `/app/data` (داخل Dockerfile) |
| `TEMP_DIR` | `/app/data/tmp` |
| `AUTO_PACK` | `0` |
| `BACKUP_ENABLED` | `1` |
| `BACKUP_INTERVAL_HOURS` | `6` |
| `BACKUP_KEEP` | `7` |
| `MAX_CONCURRENT_JOBS` | `1` |
| `ENABLE_BG_REMOVAL` | `0` |
| `LOG_LEVEL` | `INFO` |
| `RAILWAY_RUN_UID` | غير مطلوب؛ الصورة تضبط ملكية الـVolume ثم تعمل بمستخدم `app` |

لا تضع `BOT_TOKEN` في GitHub أو داخل ملف الكود. أضفه فقط في Railway Variables.

## ملاحظات التشغيل

- Volume ضروري حتى لا تضيع قاعدة SQLite والمفضلة والإعدادات وجلسات التحرير بعد إعادة النشر. Railway يركّب الـVolume بصلاحيات root، وصورة المشروع تضبط الملكية تلقائيًا عند الإقلاع ثم تعمل بمستخدم غير جذري؛ لا تحتاج أي متغير إضافي. إن واجهت `Permission denied` على نسخة قديمة أضف `RAILWAY_RUN_UID=0`.
- النسخ الاحتياطي الدوري تلقائي: كل `BACKUP_INTERVAL_HOURS` ساعة تُكتب نسخة متسقة في `/app/data/backups` بعد فحص `PRAGMA integrity_check`، ويُحتفظ بأحدث `BACKUP_KEEP` نسخة فقط. بما أن المجلد داخل Volume فإن النسخ تبقى بعد إعادة النشر. لتحميل نسخة فورية استخدم زر 📥 نسخة احتياطية في `/admin`.
- تُطبَّق ملفات `migrations/*.sql` تلقائيًا عند الإقلاع مرة واحدة، ويُسجَّل رقمها في `schema_migrations`. جميعها إضافية ولا تحذف جداول 4.0 أو بياناتها.
- `AUTO_PACK=0` يوقف إنشاء الحزم التلقائي تمامًا: التحويل يعمل وتُرسل الملفات، ولا تُنشأ حزمة إلا بأمر `/newpack` أو باختيار حزمة من `/packs`. لإعادة الحزم الدائمة التلقائية اضبط `AUTO_PACK=1`.
- استخدم Replica واحدة؛ تشغيل نسختين بنفس التوكن قد ينتج Telegram polling conflict، كما أن SQLite على Volume ليس قاعدة بيانات مشتركة للتوسع الأفقي.
- حزمة Railway تثبّت اعتماد إزالة الخلفية المحلي، لكن النموذج `u2netp` لا يُحمّل إلا عند أول استخدام. فعّل `ENABLE_BG_REMOVAL=1` لإتاحة الزر للمستخدمين؛ اتركه `0` على الموارد الصغيرة. أول عملية إزالة خلفية قد تستغرق وقتًا إضافيًا لتنزيل النموذج وتحتاج RAM أكبر.
- لا تحتاج إعداد Healthcheck HTTP لأن الخدمة لا تقدم موقعًا أو API؛ هي عامل تيليجرام طويل التشغيل.
- Railway مدفوع حسب الموارد والاستخدام، لذلك لا تعتبر هذه الحزمة ضمانًا لاستضافة مجانية دائمة.

## أوامر البوت الجديدة

- `/edit` ردًا على صورة: محرر الصور.
- `/animate` ردًا على صورة: تأثيرات Zoom وShake وFade إلى WEBM.
- `/settings`: تشغيل/إيقاف إزالة الخلفية والحزم التلقائية للمستخدم.
- `/favorite` ردًا على ملف، ثم `/favorites` لاسترجاعه.
- `/tasks`: آخر مهام المعالجة.
- `/profile`: ملخص نشاط المستخدم.
- `/packexport`: تصدير الحزمة النشطة إلى ZIP. لاختيار حزمة سابقة استخدم `/packs` ثم اختر تفعيلها أولًا.

## استكشاف الأخطاء

- `BOT_TOKEN was rejected`: تحقق من توكن BotFather.
- `Another process is already polling`: أوقف أي نسخة ثانية من البوت أو نسخة محلية تستخدم نفس التوكن.
- `ffmpeg` أو `libvpx-vp9` غير متاح: راجع Build Logs، فالصورة تثبّت FFmpeg من مستودعات Debian.
- فقدان البيانات: تحقق أن Volume مربوط على `/app/data` وليس مجلدًا آخر.
