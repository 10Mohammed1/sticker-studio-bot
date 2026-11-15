#!/bin/sh
# Railway/Compose volumes are mounted as root even when the image has an
# unprivileged user. Fix ownership of the data directory once at start, then
# drop privileges and run the bot as the normal "app" user.
set -e

if [ "$(id -u)" = "0" ]; then
    data_dir="${DATA_DIR:-/app/data}"
    temp_dir="${TEMP_DIR:-$data_dir/tmp}"
    mkdir -p "$data_dir" "$temp_dir"
    chown -R app:app "$data_dir" 2>/dev/null || true
    exec gosu app "$@"
fi

exec "$@"
