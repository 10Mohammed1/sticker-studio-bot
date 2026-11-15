# تقرير اختبار Sticker Studio 4.1.0

تاريخ آخر تحديث: 15 نوفمبر 2026

## بيئة الفحص

- Windows 11 + Python 3.12.10 داخل `.venv` المشروع.
- `aiogram 3.31.0`, `aiosqlite 0.22.1`, `Pillow 12.3.0` مثبتة.
- FFmpeg/FFprobe 9.0.2 حقيقية في `.venv/Lib/site-packages/static_ffmpeg/bin/win32/`
  وموصولة بـ `FFMPEG_PATH` و`FFPROBE_PATH` أثناء تشغيل الاختبارات (ليست في `requirements.txt`).
- لا يوجد بوت حقيقي في هذه الجلسة؛ كل تفاعلات Telegram جرت عبر `FakeTelegram` في
  `tests/test_auto_packs.py`، وهو يحاكي `createNewStickerSet` و`addStickerToSet`
  و`getStickerSet` وحدود المجموعة وأخطاء الشبكة.

## النتيجة النهائية

```
Ran 200 tests in 18.881s
OK
```

**200 اختبارًا ناجحًا، صفر فشل، صفر متخطّى.** المجموعة شُغّلت بأمر:

```powershell
$env:BOT_TOKEN="123456:TESTTOKEN"
$env:DATA_DIR="$PWD\.testdata"
$env:FFMPEG_PATH="...\ffmpeg.exe"
$env:FFPROBE_PATH="...\ffprobe.exe"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

الملف الكامل للسجل: `test-full.log`، وسجل اختبارات الحزم الدائمة: `test-autopacks.log`.

## الوضع اليدوي فقط (بعد مراجعة المستخدم)

أُضيف مسار «الحزم اليدوية فقط»: عند إيقاف الإضافة التلقائية — سواء من الزر ⚙️ أو
بـ`AUTO_PACK=0` — لا تُنشأ أي حزمة من تلقاء نفسها، ويبقى `/newpack` (و`/packs → use`)
هو الذي ينشئ الحزمة ويضيف إليها. الاختبارات الثلاثة المضافة:

- `test_disabled_auto_add_creates_no_pack_for_a_plain_photo`
- `test_disabled_auto_add_still_feeds_a_newpack_pack`
- `test_host_kill_switch_still_allows_a_manual_pack`

## فحص حي على تيليجرام (بوت حقيقي)

- `getMe` نجح: `@eiehevjebot` (`id=8537043257`).
- الإقلاع من الصفر طبّق `004` و`005` ثم كتب نسخة احتياطية مفحوصة.
- أول صورة أرسلها المستخدم أُنشئت لها حزمة ثابتة فعلية:
  `s7167097_e68b9f1b_by_eiehevjebot`، وأكد `getStickerSet` وجود ملصق واحد
  بمقاس **512×512** ونوع `regular`، وسُجّل `last_added_at` في القاعدة.
- بعد تغيير الوضع إلى `AUTO_PACK=0` أُعيد تشغيل البوت ونجح الاتصال، وأصبح التحويل
  يعمل دون إنشاء حزم تلقائية.

## الاختبارات الـ13 المطلوبة في مواصفة 4.1

الملف `tests/test_auto_packs.py` يحتوي **39 اختبارًا** تغطي السيناريوهات المطلوبة:

| # | السيناريو | الاختبار |
|---|---|---|
| 1 | أول صورة → إنشاء الحزمة الثابتة | `test_01_first_image_creates_the_static_pack` |
| 2 | الصورة الثانية → نفس الحزمة دون set جديد | `test_02_second_image_reuses_the_same_pack` |
| 3 | أول GIF → الحزمة المتحركة | `test_03_first_gif_creates_the_animated_pack` |
| 4 | ثاني ملف متحرك → نفس الحزمة المتحركة | `test_04_second_animated_file_reuses_the_animated_pack` |
| 5 | مستخدمان → حزمتان مستقلتان | `test_05_two_users_never_share_a_pack` |
| 6 | إعادة التشغيل → الحزم محفوظة ومُعاد استخدامها | `test_06_packs_survive_a_process_restart` |
| 7 | إيقاف الإضافة التلقائية → يُحفظ في القاعدة | `test_07_disabling_auto_add_is_persisted` |
| 8 | التحويل يستمر أثناء الإيقاف | `test_08_conversion_continues_while_disabled` |
| 9 | إعادة التفعيل → نفس الحزمة | `test_09_reenabling_adds_to_the_same_pack` |
| 10 | انقطاع الشبكة أثناء الإضافة | `test_10a` … `test_10e` |
| 11 | الملف المكرر (حتى باسم آخر) | `test_11`, `test_same_bytes_with_new_name_is_still_a_duplicate` |
| 12 | حد 50 ملصقًا → فتح جزء ثانٍ دون حذف القديم | `test_12_full_pack_opens_part_two`, `test_full_pack_from_telegram_error_rotates` |
| 13 | ترقية مخطط 4.0 → 4.1 دون فقدان بيانات | `test_13_existing_4_0_data_survives_and_is_adopted` |

### تفاصيل السيناريو 10 (الفشل والتعافي)

- `test_10a` — خطأ شبكة عابر ثم نجاح: إعادة محاولة واحدة وإضافة واحدة.
- `test_10b` — الرد ضاع بعد نجاح `addStickerToSet`: تُقرأ الحزمة الحية ويُعتمد النمو،
  فلا يُضاف الملصق مرتين (`count 1 -> 2`).
- `test_10c` — انقطاع مستمر: `failed` مع رسالة `⚠️`، ولا كتابة في `user_packs`
  ولا `sticker_dedupe`، وسجل التحويل يبقى `failed`.
- `test_10d` — **الرد ضاع بعد إنشاء الحزمة**: لا يتغيّر اسم الحزمة بين المحاولات،
  فيُعاد قراءة الاسم نفسه ويُعتمد، والنتيجة **set واحد فقط** و`create_calls == 1`
  ولا حزمة يتيمة.
- `test_10e` — اسم الحزمة مملوك لشخص آخر: لا يُكتب فيه أبدًا، ويُختار اسم جديد
  ويُنشأ في الحزمة الجديدة فقط.

### اختبارات التحويل الحقيقي → الحزمة الدائمة

تعمل على ملفات حقيقية وFFmpeg حقيقي، ثم تمرّر الناتج إلى `handle_outputs`:

- `test_static_png_becomes_transparent_webp_and_joins_static_pack`:
  PNG 320×240 → WEBP 512×512، البكسل `(0,0)` شفاف تمامًا (alpha = 0)،
  ثم يُضاف إلى الحزمة الثابتة بصيغة `static`.
- `test_gif_becomes_vp9_webm_and_joins_animated_pack`:
  GIF شفاف → `loop.webm`، توقيع EBML صحيح، `codec_name=vp9`, `width=512`,
  وبعد فك أول إطار: الزاوية alpha = 0 والمركز alpha = 255 (الشفافية محفوظة)،
  ثم يُضاف إلى الحزمة `video`.
- `test_mp4_becomes_video_sticker`:
  MP4 256×256 → WEBM/VP9 ثم الحزمة المتحركة.

### إضافات ما بعد المراجعة

- `test_last_added_at_is_persisted_after_confirmation`: بعد تأكيد Telegram فقط
  تُكتب `sticker_count` و`last_file_unique_id` و`last_added_at` غير الفارغ.
- `ExplicitPackCompatibilityTests` (اختباران): `/newpack` و`/packs → use` يعملان
  كتجاوز اختياري (override) ويستقبلان الناتج في الحزمة المخصصة، والحزمة الدائمة
  تبقى فارغة، و`/endpack` يعيد الوضع الافتراضي. التكرار بـ SHA-256 يُحترم أيضًا.
- `BackupTests` (3 اختبارات): النسخة الدورية تُبنى بـ SQLite online backup،
  تحتوي المستخدمين والمفضلة، تنجح `PRAGMA integrity_check`، وتُقلَّم الأقدم
  مع بقاء قاعدة البيانات الحية سليمة. قاعدة مفقودة تُرفع `FileNotFoundError`.
- `MigrationTests` (3 اختبارات): `schema_migrations` يسجّل `{4, 5}` مرة واحدة،
  وإعادة `init()` أو`apply_migrations()` لا تكرر التطبيق، والجداول القديمة سليمة.

## باقي مجموعات الاختبارات (161 اختبارًا)

- توليد WEBM حقيقي بـ FFmpeg (VP9 + شفافية) للتأثيرات: zoom, zoom_out, shake, pulse,
  slide_left, fade, fade_in, fade_out, rotate — كلها 512×512 و≤ 3 ثوانٍ و≤ 256KB.
- تحرير الصور بالـ17 أداة على صور اختبارية، والتحقق من 512×512 وRGBA.
- تطبيق قوالب التحرير من الأصل والتحقق من النتيجة الحتمية.
- طابور المهام: الأولويات، التقدّم، الإلغاء، إعادة المحاولة، منع التكرار،
  حد المهام لكل مستخدم، وعدم التعارض بين `slot()` و`run_tracked`.
- Pack Manager بواجهة Bot وهمية: حذف/نقل/نسخ/دمج، رفض الحزم غير المملوكة،
  رفض خلط الصيغ، حد 50 ملصقًا، وكشف التكرارات.
- قاعدة البيانات: `pack_items` بالترتيب، `get_task`، القوالب، إعدادات وقت التشغيل،
  وسجل المهام الفاشلة، و`get_conversions`.
- المراقبة: RAM ومنصات مختلفة (psutil/ctypes/proc)، القرص، وحجم البيانات.
- التنظيف: حذف الملفات القديمة فقط، وتقليم جلسات التحرير المهجورة.
- الفحص الأمني، الحدود، الإعدادات، والإدارة.

## اختبارات تحتاج جهازًا/حسابًا حقيقيًا

- تعديل حزمة حقيقية في تيليجرام (حذف/ترتيب/نسخ/دمج) يحتاج `ADMIN_IDS` مضبوطًا
  وحزمة أنشأها البوت نفسه.
- إنشاء حزم فعلية في تيليجرام: يحتاج `BOT_TOKEN` حقيقيًا ومحادثة مفتوحة مع البوت؛
  المنطق نفسه مُختبَر بالكامل على `FakeTelegram` بنفس توقيعات aiogram.
- بناء Docker فعليًا: Docker CLI غير متاح في هذه الجلسة (ملفات Dockerfile موجودة).
- النشر على Railway: يحتاج توكن وVolume؛ متطلباته موثّقة في `RAILWAY-DEPLOY-AR.md`.

## حدود معروفة

- البوت يعدّل فقط الحزم التي أنشأها هو (`*_by_<bot_username>`)، وهذا قيد تيليجرام.
- لا خلط بين WEBP/WEBM/TGS داخل حزمة واحدة، والحد 50 ملصقًا لكل جزء.
- المهمة الجارية لا تُستأنف بعد إعادة تشغيل الخدمة؛ تُعاد من المستخدم أو بزر إعادة المحاولة.
- القوالب تعيد أدوات Pillow فقط، ولا تشمل إزالة الخلفية أو Light AI.
- إزالة الخلفية تبقى اختيارية (`ENABLE_BG_REMOVAL` + حزمة rembg) وقد لا تناسب
  الاستضافات الصغيرة؛ Light AI يعمل دائمًا بلا نماذج.
- لا يوجد توليد صور كامل محليًا داخل 4.1.0، ولا واجهة إنجليزية كاملة.
