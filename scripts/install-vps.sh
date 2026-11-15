#!/usr/bin/env bash
# Sticker Studio 4.1.1 — one-command installer for a fresh Ubuntu/Debian VPS.
#
#   bash install-vps.sh
#
# Optional (fully non-interactive):
#   BOT_TOKEN=... ADMIN_IDS=... ADMIN_PASSWORD=... bash install-vps.sh
#
# It installs Docker, creates a sudo user (default: admin), clones the public
# repository into /opt/sticker-studio, writes a .env with the bot token and the
# owner id, and starts the bot with a persistent volume. Re-running it pulls the
# latest code and restarts the service without touching the data.
set -euo pipefail

REPO_URL="https://github.com/10Mohammed1/sticker-studio-bot.git"
APP_DIR="/opt/sticker-studio"
APP_USER="${ADMIN_USER:-admin}"

say() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "شغّل السكربت بصلاحية root: sudo bash install-vps.sh"
command -v apt-get >/dev/null 2>&1 || fail "السكربت يدعم Ubuntu/Debian فقط."

# Ask a question on the terminal even when the script was piped into bash.
ask() {
    local prompt="$1" __out="$2" answer=""
    if [ -r /dev/tty ]; then
        printf '%s' "$prompt" > /dev/tty
        IFS= read -r answer < /dev/tty || true
    else
        printf '%s' "$prompt" >&2
        IFS= read -r answer || true
    fi
    printf -v "$__out" '%s' "$answer"
}

say "1/6 تثبيت الحزم الأساسية"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git sudo openssl >/dev/null

say "2/6 تثبيت Docker (إن لم يكن مثبتًا)"
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
else
    echo "Docker موجود مسبقًا: $(docker --version)"
fi
systemctl enable --now docker >/dev/null 2>&1 || true

say "3/6 تجهيز المستخدم: $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "$APP_USER"
    usermod -aG sudo "$APP_USER"
    usermod -aG docker "$APP_USER"
    echo "أُنشئ المستخدم $APP_USER مع صلاحية sudo."
else
    usermod -aG sudo,docker "$APP_USER" 2>/dev/null || true
    echo "المستخدم $APP_USER موجود مسبقًا."
fi

# Password: ADMIN_PASSWORD if provided, otherwise a strong random one that is
# printed once at the end. "admin/admin" is never used on a public server.
GENERATED_PASSWORD=""
if [ -n "${ADMIN_PASSWORD:-}" ]; then
    PASS="$ADMIN_PASSWORD"
else
    PASS="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-18)"
    GENERATED_PASSWORD="$PASS"
fi
echo "$APP_USER:$PASS" | chpasswd
echo "تم تعيين كلمة مرور قوية للمستخدم $APP_USER."
unset PASS

if [ ! -s /root/.ssh/authorized_keys ] && [ -s "/home/$APP_USER/.ssh/authorized_keys" ]; then
    install -d -m 700 -o root -g root /root/.ssh
    cp "/home/$APP_USER/.ssh/authorized_keys" /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi

say "4/6 جلب المشروع"
mkdir -p "$(dirname "$APP_DIR")"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "5/6 إعداد المتغيّرين المطلوبين"
if [ -f "$APP_DIR/.env" ]; then
    echo "يوجد ملف .env مسبقًا، سيُستخدم كما هو (احذفه لإعادة الإدخال)."
else
    TOKEN="${BOT_TOKEN:-}"
    [ -n "$TOKEN" ] || ask 'ألصق BOT_TOKEN من @BotFather: ' TOKEN
    [ -n "$TOKEN" ] || fail "التوكن مطلوب."

    OWNER="${ADMIN_IDS:-}"
    [ -n "$OWNER" ] || ask 'ألصق ADMIN_IDS (رقمك من /i أو @userinfobot): ' OWNER
    [ -n "$OWNER" ] || fail "رقم المالك مطلوب."

    umask 077
    cat > "$APP_DIR/.env" <<EOF
BOT_TOKEN=$TOKEN
ADMIN_IDS=$OWNER
DATA_DIR=/app/data
TEMP_DIR=/app/data/tmp
AUTO_PACK=0
BACKUP_ENABLED=1
EOF
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    unset TOKEN OWNER
fi

say "6/6 بناء وتشغيل الحاوية"
cd "$APP_DIR"
docker compose up -d --build

say "تم التشغيل ✔"
docker compose ps
echo
echo "آخر السجلات:"
docker compose logs --tail 25 || true
echo
if [ -n "$GENERATED_PASSWORD" ]; then
    echo "════════════════════════════════════════════════"
    echo " بيانات الدخول إلى السيرفر (تُعرض مرة واحدة فقط)"
    echo "   المستخدم : $APP_USER"
    echo "   كلمة السر: $GENERATED_PASSWORD"
    echo "   الأمر    : ssh $APP_USER@$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo " احفظها الآن ثم غيّرها بـ: passwd"
    echo "════════════════════════════════════════════════"
fi
echo
echo "متابعة السجلات:  cd $APP_DIR && docker compose logs -f"
echo "إعادة التشغيل:   cd $APP_DIR && docker compose restart"
echo "التحديث لاحقًا:  cd $APP_DIR && git pull && docker compose up -d --build"
