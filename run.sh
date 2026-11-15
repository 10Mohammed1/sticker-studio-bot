#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -d .venv ]; then
    echo "[1/3] Creating the virtual environment..."
    "$PYTHON" -m venv .venv
fi

echo "[2/3] Installing requirements..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "A new .env file was created. Set BOT_TOKEN and ADMIN_IDS in it, then run this script again."
    exit 1
fi

echo "[3/3] Starting Sticker Studio..."
echo "Optional extras: requirements-pdf.txt, requirements-vector.txt, requirements-ai.txt"
exec .venv/bin/python -m bot.main
