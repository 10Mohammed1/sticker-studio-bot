# نشر Sticker Studio 4.1.1 على VPS

> لا أستطيع إنشاء سيرفر أو منحك يوزر/باسورد لجهاز لا أملكه. ما أستطيعه هو تجهيز
> تثبيت كامل بأمر واحد على أي VPS تملكه أنت. السكربت جاهز في المستودع.

## 1) اطلب سيرفرًا

أي مزوّد يعطي Ubuntu 22.04/24.04 يعمل:

- 1 vCPU و1 GB RAM كافية (يُفضّل 2 GB إن كان الاستخدام كثيفًا).
- 10 GB قرص.
- لا تحتاج Domain ولا منفذًا مفتوحًا غير SSH.

أمثلة: Hetzner CX22، Contabo VPS S، DigitalOcean Basic، Vultr.

بعد الإنشاء يصلك: **IP** و**كلمة مرور root** (أو مفتاح SSH).\n\n> المثبّت (الخطوة 3) ينشئ مستخدم `admin` بصلاحية sudo وDocker، ويولّد له كلمة مرور قوية (تُطبع مرة واحدة). لإجبار كلمة مرور مخصّصة (مثلاً `admin`) شغّل: `ADMIN_PASSWORD=admin bash install-vps.sh` — **غير مُوصى به على سيرفر عام**.

## 2) ادخل إلى السيرفر

```bash
ssh root@YOUR_SERVER_IP
```

## 3) شغّل المثبّت

```bash
curl -fsSL https://raw.githubusercontent.com/10Mohammed1/sticker-studio-bot/main/scripts/install-vps.sh -o install.sh
bash install.sh
```

سيسألك شيئين فقط:

```
BOT_TOKEN: 8537...:AA...
ADMIN_IDS: 8767167097
```

ثم يسألك عن كلمة مرور للمستخدم `admin` (يمكن تركها فارغة إن كنت ستستخدم مفتاح SSH).

## ما الذي يفعله السكربت؟

1. يثبّت الحزم الأساسية وDocker (إن لم يكن مثبتًا).
2. ينشئ مستخدم `admin` بصلاحية `sudo` ومجموعة `docker`.
3. ينسخ المشروع إلى `/opt/sticker-studio`.
4. يكتب `.env` بصلاحية `600` يحتوي التوكن والرقم فقط.
5. يشغّل `docker compose up -d --build` مع حجم دائم `sticker_data` على `/app/data`.
6. يطبع حالة الخدمة وآخر 25 سطرًا من السجلات.

إعادة تشغيل السكربت تحدّث الكود (`git pull`) وتعيد البناء دون فقدان البيانات.

## 4) تأكّد من النجاح

```bash
cd /opt/sticker-studio
docker compose logs -f
```

ابحث عن:

```
applied database migration 004_studio_v4.sql
applied database migration 005_auto_packs.sql
Sticker Studio started as @your_bot
```

ثم من تيليجرام أرسل `/start` و`/i`، وضع رقمك في `ADMIN_IDS` إن اختلف.

## 5) تأمين السيرفر (مهم)

```bash
# من جهازك: انسخ مفتاحك العام
ssh-copy-id admin@YOUR_SERVER_IP

# ثم على السيرفر: امنع دخول root وكلمة المرور
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# جدار ناري بسيط
apt-get install -y ufw
ufw allow 22/tcp
ufw --force enable
```

لا تستخدم `admin/admin`؛ كلمة مرور ضعيفة على VPS عام تُخترق خلال ساعات.

## أوامر الإدارة اليومية

```bash
cd /opt/sticker-studio
docker compose ps                 # الحالة
docker compose logs -f            # السجلات مباشرة
docker compose restart            # إعادة تشغيل
docker compose down               # إيقاف
git pull && docker compose up -d --build   # تحديث
```

## البيانات والنسخ الاحتياطي

- قاعدة البيانات والحزم: الحجم `sticker_data` داخل الحاوية على `/app/data`.
- نسخة دورية مفحوصة: `/app/data/backups` (كل 6 ساعات، يحتفظ بآخر 7).
- نسخة يدوية كاملة:

```bash
docker run --rm -v sticker-studio_sticker_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/sticker-data-backup.tar.gz -C /data .
```

## استكشاف الأخطاء

| العرض | الحل |
|---|---|
| `Permission denied /app/data` | تأكد أنك على آخر نسخة (entrypoint يصلح الملكية) أو `docker compose down && up -d` |
| `Conflict: terminated by other getUpdates` | نسخة أخرى تعمل بنفس التوكن؛ أوقفها |
| `BOT_TOKEN was rejected` | `/revoke` من BotFather ثم توكن جديد في `.env` |
| البوت لا يرد | `docker compose logs --tail 100` وتحقق من الاتصال |
