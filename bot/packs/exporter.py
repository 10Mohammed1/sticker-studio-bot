"""Export an existing Telegram sticker set to a portable ZIP archive."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any


class PackExportError(RuntimeError):
    pass


async def export_pack(bot: Any, pack_name: str, destination: Path) -> dict[str, Any]:
    try:
        sticker_set = await bot.get_sticker_set(name=pack_name)
    except Exception as exc:
        raise PackExportError("لم أستطع قراءة حزمة الملصقات من تيليجرام.") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "name": sticker_set.name,
        "title": sticker_set.title,
        "sticker_count": len(sticker_set.stickers),
        "stickers": [],
    }
    downloaded: list[tuple[Path, str]] = []
    for index, sticker in enumerate(sticker_set.stickers, start=1):
        extension = ".tgs" if sticker.is_animated else ".webm" if sticker.is_video else ".webp"
        filename = f"stickers/{index:03d}{extension}"
        local_path = destination.parent / f"_{destination.stem}_{index:03d}{extension}"
        try:
            telegram_file = await bot.get_file(sticker.file_id)
            if not telegram_file.file_path:
                raise PackExportError("تيليجرام لم يُرجع مسار أحد الملصقات.")
            await bot.download_file(telegram_file.file_path, destination=local_path)
        except Exception as exc:
            for path, _ in downloaded:
                path.unlink(missing_ok=True)
            local_path.unlink(missing_ok=True)
            if isinstance(exc, PackExportError):
                raise
            raise PackExportError(f"تعذر تنزيل الملصق رقم {index} من الحزمة.") from exc
        downloaded.append((local_path, filename))
        manifest["stickers"].append({
            "file": filename,
            "emoji": getattr(sticker, "emoji", None),
            "is_animated": bool(sticker.is_animated),
            "is_video": bool(sticker.is_video),
        })

    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("stickerstudio-pack.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for path, archive_name in downloaded:
                archive.write(path, arcname=archive_name)
    finally:
        for path, _ in downloaded:
            path.unlink(missing_ok=True)
    return manifest
