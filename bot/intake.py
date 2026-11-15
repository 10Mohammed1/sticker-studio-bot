"""Safe Telegram media intake (aiogram edition).

Every incoming Telegram media object is downloaded once and normalized to a
local file whose extension matches its *actual* content (magic bytes + MIME),
not merely the extension supplied by the sender. Conversion stays in
``converter.py``; this module only answers:

    * which Telegram object should be downloaded (e.g. the largest PhotoSize)?
    * where is the local file, and what is it really?
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramRetryAfter
from PIL import Image, UnidentifiedImageError

log = logging.getLogger("sticker-studio.intake")

MAX_SNIFF_BYTES = 16 * 1024


class MediaIntakeError(Exception):
    """Expected intake failure carrying a safe, user-facing Arabic message."""


class MediaDownloadError(MediaIntakeError):
    """Telegram could not deliver the file (network/size/empty file)."""


class UnsupportedMediaError(MediaIntakeError):
    """The downloaded bytes are not a supported media type."""


@dataclass(frozen=True)
class MediaInfo:
    """A downloaded, content-verified local upload."""

    path: Path
    original_name: str
    name: str          # safe local name with the detected extension
    mime: str
    kind: str          # image | animation | video | audio | vector | pdf | tgs | archive | unknown
    size: int
    sha256: str
    telegram_kind: str  # photo | document | video | animation | video_note | sticker | audio | voice


# extension -> (mime, kind)
_EXT_TYPES: dict[str, tuple[str, str]] = {
    ".png": ("image/png", "image"),
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".jfif": ("image/jpeg", "image"),
    ".webp": ("image/webp", "image"),
    ".bmp": ("image/bmp", "image"),
    ".tif": ("image/tiff", "image"),
    ".tiff": ("image/tiff", "image"),
    ".avif": ("image/avif", "image"),
    ".gif": ("image/gif", "animation"),
    ".apng": ("image/apng", "animation"),
    ".mp4": ("video/mp4", "video"),
    ".m4v": ("video/mp4", "video"),
    ".mov": ("video/quicktime", "video"),
    ".mkv": ("video/x-matroska", "video"),
    ".webm": ("video/webm", "video"),
    ".avi": ("video/x-msvideo", "video"),
    ".mpeg": ("video/mpeg", "video"),
    ".mpg": ("video/mpeg", "video"),
    ".3gp": ("video/3gpp", "video"),
    ".mp3": ("audio/mpeg", "audio"),
    ".wav": ("audio/wav", "audio"),
    ".ogg": ("audio/ogg", "audio"),
    ".oga": ("audio/ogg", "audio"),
    ".opus": ("audio/opus", "audio"),
    ".m4a": ("audio/mp4", "audio"),
    ".aac": ("audio/aac", "audio"),
    ".flac": ("audio/flac", "audio"),
    ".wma": ("audio/x-ms-wma", "audio"),
    ".amr": ("audio/amr", "audio"),
    ".svg": ("image/svg+xml", "vector"),
    ".pdf": ("application/pdf", "pdf"),
    ".tgs": ("application/x-tgsticker", "tgs"),
    ".json": ("application/json", "lottie"),
    ".zip": ("application/zip", "archive"),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "archive"),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "archive"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "archive"),
    ".odt": ("application/vnd.oasis.opendocument.text", "archive"),
    ".odp": ("application/vnd.oasis.opendocument.presentation", "archive"),
    ".ods": ("application/vnd.oasis.opendocument.spreadsheet", "archive"),
}

_EXT_ALIASES = {
    ".jpeg": {".jpg", ".jpeg", ".jfif"},
    ".jpg": {".jpg", ".jpeg", ".jfif"},
    ".tif": {".tif", ".tiff"},
    ".tiff": {".tif", ".tiff"},
}


class _Sniffer:
    """Small, dependency-free magic-byte sniffer for the formats we accept."""

    def __init__(self, path: Path, original_name: str, declared_mime: str | None):
        self.path = path
        self.original_name = original_name
        self.declared_mime = (declared_mime or "").lower().strip()
        self.suffix = Path(original_name).suffix.lower()

    def detect(self) -> tuple[str, str, str]:
        try:
            with self.path.open("rb") as stream:
                head = stream.read(MAX_SNIFF_BYTES)
        except OSError as exc:
            raise MediaDownloadError("تعذر قراءة الملف الذي تم تنزيله.") from exc
        if not head:
            raise MediaDownloadError("الملف وصل فارغًا من تيليجرام. أعد إرساله.")

        # --- unambiguous binary signatures ---------------------------------
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png", "image/png", "image"
        if head.startswith(b"\xff\xd8\xff"):
            return ".jpg", "image/jpeg", "image"
        if head.startswith((b"GIF87a", b"GIF89a")):
            return ".gif", "image/gif", "animation"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp", "image/webp", "image"
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return ".wav", "audio/wav", "audio"
        if head.startswith(b"BM"):
            return ".bmp", "image/bmp", "image"
        if head.startswith((b"II*\x00", b"MM\x00*")):
            return ".tiff", "image/tiff", "image"
        if head.startswith(b"%PDF"):
            return ".pdf", "application/pdf", "pdf"
        if head.startswith(b"fLaC"):
            return ".flac", "audio/flac", "audio"
        if head.startswith(b"OggS"):
            return ".ogg", "audio/ogg", "audio"
        if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
            return ".mp3", "audio/mpeg", "audio"
        if head.startswith(b"#!AMR"):
            return ".amr", "audio/amr", "audio"
        if head[4:8] == b"ftyp":
            brand = head[8:12]
            if brand in {b"qt  ", b"QT  "}:
                return ".mov", "video/quicktime", "video"
            if brand in {b"M4A ", b"M4B ", b"M4P "}:
                return ".m4a", "audio/mp4", "audio"
            return ".mp4", "video/mp4", "video"
        if head.startswith(b"\x1aE\xdf\xa3"):
            return ".webm", "video/webm", "video"
        if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
            return self._detect_zip()

        # --- text-based formats -------------------------------------------
        text_head = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if text_head.startswith(b"<svg") or (
            text_head.startswith(b"<?xml") and b"<svg" in text_head
        ):
            return ".svg", "image/svg+xml", "vector"
        if self.suffix == ".json" or text_head.startswith(b"{"):
            if self._detect_lottie():
                return ".json", "application/json", "lottie"

        # --- gzip containers (TGS / Lottie) -------------------------------
        if head.startswith(b"\x1f\x8b"):
            return self._detect_gzip()

        # --- Pillow fallback (odd but valid image encodings) --------------
        detected = self._detect_with_pillow()
        if detected:
            return detected

        # --- declared hints only after real content checks ----------------
        if self.suffix in _EXT_TYPES:
            expected_mime = _EXT_TYPES[self.suffix][0]
            if self.declared_mime in {
                "",
                "application/octet-stream",
                "binary/octet-stream",
                expected_mime,
            }:
                mime, kind = _EXT_TYPES[self.suffix]
                return self.suffix, mime, kind
        if self.declared_mime in {"image/svg+xml"}:
            return ".svg", "image/svg+xml", "vector"

        raise UnsupportedMediaError(
            "نوع الملف غير مدعوم. أرسل صورة أو GIF أو فيديو أو SVG أو PDF أو ZIP "
            "أو Word/PowerPoint يحتوي على صور أو ملف TGS."
        )

    def _detect_zip(self) -> tuple[str, str, str]:
        """Distinguish real ZIP archives from Office/OpenDocument containers."""
        try:
            with zipfile.ZipFile(self.path) as archive:
                names = set(archive.namelist()[:300])
                if "word/document.xml" in names:
                    return ".docx", _EXT_TYPES[".docx"][0], "archive"
                if "ppt/presentation.xml" in names:
                    return ".pptx", _EXT_TYPES[".pptx"][0], "archive"
                if "xl/workbook.xml" in names:
                    return ".xlsx", _EXT_TYPES[".xlsx"][0], "archive"
                if "mimetype" in names:
                    try:
                        mimetype = archive.read("mimetype").decode("ascii", "ignore").strip()
                    except Exception:
                        mimetype = ""
                    odf = {
                        "application/vnd.oasis.opendocument.text": ".odt",
                        "application/vnd.oasis.opendocument.presentation": ".odp",
                        "application/vnd.oasis.opendocument.spreadsheet": ".ods",
                    }.get(mimetype)
                    if odf:
                        return odf, mimetype, "archive"
        except (zipfile.BadZipFile, OSError, RuntimeError):
            raise UnsupportedMediaError("ملف الأرشيف تالف أو غير قابل للقراءة.") from None
        # Respect a declared Office extension for a valid ZIP container.
        if self.suffix in {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods"}:
            return self.suffix, _EXT_TYPES[self.suffix][0], "archive"
        return ".zip", "application/zip", "archive"

    def _detect_gzip(self) -> tuple[str, str, str]:
        try:
            with gzip.open(self.path, "rb") as stream:
                payload = stream.read(MAX_SNIFF_BYTES)
        except (OSError, EOFError) as exc:
            raise UnsupportedMediaError("الملف المضغوط تالف أو غير مكتمل.") from exc
        if b'"layers"' in payload and b'"fr"' in payload:
            return ".tgs", "application/x-tgsticker", "tgs"
        if self.suffix == ".tgs":
            # The converter performs the authoritative Lottie validation.
            return ".tgs", "application/x-tgsticker", "tgs"
        raise UnsupportedMediaError(
            "ملف gzip غير مدعوم. أرسل ملف TGS من تيليجرام أو صورة/فيديو مباشرة."
        )

    def _detect_lottie(self) -> bool:
        """Recognize a Bodymovin/Lottie JSON document by its required keys."""
        try:
            if self.path.stat().st_size > 2 * 1024 * 1024:
                return False
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        required = ("v", "fr", "ip", "op", "w", "h", "layers")
        return isinstance(data, dict) and all(key in data for key in required)

    def _detect_with_pillow(self) -> tuple[str, str, str] | None:
        by_format = {
            "PNG": (".png", "image/png", "image"),
            "JPEG": (".jpg", "image/jpeg", "image"),
            "WEBP": (".webp", "image/webp", "image"),
            "GIF": (".gif", "image/gif", "animation"),
            "BMP": (".bmp", "image/bmp", "image"),
            "TIFF": (".tiff", "image/tiff", "image"),
            "AVIF": (".avif", "image/avif", "image"),
        }
        try:
            with Image.open(self.path) as image:
                fmt = (image.format or "").upper()
                if fmt in by_format:
                    return by_format[fmt]
        except (UnidentifiedImageError, OSError, ValueError):
            return None
        return None


def _safe_name(name: str, fallback: str = "upload") -> str:
    # Path(...).name strips both / and \ on Windows and treats traversal as a name.
    base = Path(str(name).replace("\\", "/")).name
    base = re.sub(r"[^\w.()\- ]+", "_", base, flags=re.UNICODE).strip(" .")
    base = base[:100] or fallback
    if base.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        base = "_" + base
    return base


def _free_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(1, 1000):
        alternative = candidate.with_name(f"{stem}_{index}{suffix}")
        if not alternative.exists():
            return alternative
    return candidate.with_name(f"{stem}_{hashlib.sha1(name.encode()).hexdigest()[:8]}{suffix}")


def _with_detected_suffix(path: Path, detected_suffix: str) -> Path:
    """Rename a download when its bytes disagree with its name.

    ``photo.jpg`` stays put; ``photo.txt`` holding JPEG bytes becomes
    ``photo.jpg`` so the conversion engine dispatches on the truth.
    """
    current = path.suffix.lower()
    if current == detected_suffix or current in _EXT_ALIASES.get(detected_suffix, set()):
        return path
    target = _free_path(path.parent, path.stem + detected_suffix)
    try:
        path.replace(target)
    except OSError as exc:
        raise MediaDownloadError("تعذر تجهيز اسم الملف المؤقت بعد التنزيل.") from exc
    return target


def select_media(message: Any) -> tuple[Any, str, str | None, str]:
    """Pick the right Telegram attachment and a provisional name/MIME."""
    if message.photo:
        # Telegram PhotoSize tuples are usually smallest -> largest, but never
        # trust the order: pick the one with the largest pixel area.
        largest = max(
            message.photo,
            key=lambda item: (
                int(getattr(item, "width", 0) or 0) * int(getattr(item, "height", 0) or 0),
                int(getattr(item, "file_size", 0) or 0),
            ),
        )
        return largest, "telegram_photo.jpg", "image/jpeg", "photo"
    if message.document:
        document = message.document
        name = document.file_name or "telegram_document"
        mime = document.mime_type or mimetypes.guess_type(name)[0]
        return document, name, mime, "document"
    if message.video:
        video = message.video
        return video, video.file_name or "telegram_video.mp4", video.mime_type or "video/mp4", "video"
    if message.animation:
        animation = message.animation
        return (
            animation,
            animation.file_name or "telegram_animation.mp4",
            animation.mime_type or "video/mp4",
            "animation",
        )
    if message.video_note:
        return message.video_note, "telegram_video_note.mp4", "video/mp4", "video_note"
    if message.audio:
        audio = message.audio
        return audio, audio.file_name or "telegram_audio.mp3", audio.mime_type or "audio/mpeg", "audio"
    if message.voice:
        voice = message.voice
        return voice, "telegram_voice.ogg", voice.mime_type or "audio/ogg", "voice"
    if message.sticker:
        sticker = message.sticker
        if sticker.is_animated:
            return sticker, "telegram_sticker.tgs", "application/x-tgsticker", "sticker"
        if sticker.is_video:
            return sticker, "telegram_sticker.webm", "video/webm", "sticker"
        return sticker, "telegram_sticker.webp", "image/webp", "sticker"
    return None, "", None, ""


async def _get_file_with_retry(bot: Any, file_id: str, trace_id: str) -> Any:
    last_error: Exception | None = None
    # getFile is a small metadata call; a short timeout keeps failures quick.
    for attempt in range(1, 3):
        try:
            return await bot.get_file(file_id, request_timeout=25)
        except TelegramRetryAfter as exc:
            last_error = exc
            wait = float(exc.retry_after) + 1.0
            log.warning("[%s] get_file rate-limited; waiting %.1fs", trace_id, wait)
            await asyncio.sleep(wait)
        except Exception as exc:  # TelegramError / network / timeout
            last_error = exc
            log.warning("[%s] get_file attempt %d/2 failed: %s", trace_id, attempt, exc)
            if attempt < 2:
                await asyncio.sleep(2 * attempt)
    raise MediaDownloadError(
        "تعذر الوصول إلى الملف من تيليجرام بعد محاولتين. "
        "قد تكون الشبكة غير مستقرة — أعد إرسال الملف."
    ) from last_error


async def _download_with_retry(bot: Any, file_path: str, target: Path, trace_id: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            await bot.download_file(file_path, destination=target, timeout=60)
            if target.exists() and target.stat().st_size > 0:
                return
        except TelegramRetryAfter as exc:
            last_error = exc
            wait = float(exc.retry_after) + 1.0
            log.warning("[%s] download rate-limited; waiting %.1fs", trace_id, wait)
            target.unlink(missing_ok=True)
            await asyncio.sleep(wait)
            continue
        except Exception as exc:
            last_error = exc
            log.warning("[%s] download attempt %d/2 failed: %s", trace_id, attempt, exc)
        target.unlink(missing_ok=True)
        if attempt < 2:
            await asyncio.sleep(2 * attempt)
    raise MediaDownloadError(
        "تعذر تنزيل الملف من تيليجرام بعد محاولتين. "
        "قد يكون الملف كبيرًا أو الشبكة غير مستقرة — أعد المحاولة."
    ) from last_error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def receive_media(
    message: Any,
    bot: Any,
    directory: Path,
    settings: Any,
    trace_id: str = "-",
) -> MediaInfo:
    """Download the best attachment from ``message`` and verify its real type.

    This is the single intake path for photo/document/video/animation/
    video_note/audio/voice/sticker messages. It never returns a Telegram
    ``file_id`` pretending to be a local path.
    """
    tg_obj, original_name, declared_mime, telegram_kind = select_media(message)
    if tg_obj is None:
        raise UnsupportedMediaError("الرسالة لا تحتوي على ملف قابل للتحويل.")

    directory.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1, int(settings.max_download_mb)) * 1024 * 1024

    temporary_name = _safe_name(original_name, fallback=f"{telegram_kind or 'upload'}.bin")
    if not Path(temporary_name).suffix:
        temporary_name += ".bin"
    target = _free_path(directory, temporary_name)

    tg_file = await _get_file_with_retry(bot, tg_obj.file_id, trace_id)
    announced_size = int(getattr(tg_obj, "file_size", 0) or 0) or int(
        getattr(tg_file, "file_size", 0) or 0
    )
    if announced_size and announced_size > max_bytes:
        raise MediaDownloadError(
            f"حجم الملف أكبر من {settings.max_download_mb} MB (حدود تنزيل Telegram Bot API). "
            "اضغطه أو أرسل نسخة أصغر."
        )

    log.info(
        "[%s] %s received: declared=%r mime=%r size=%s",
        trace_id, telegram_kind, original_name, declared_mime, announced_size or "?",
    )
    if not tg_file.file_path:
        raise MediaDownloadError("تعذر الحصول على مسار الملف من تيليجرام.")
    await _download_with_retry(bot, tg_file.file_path, target, trace_id)

    size = target.stat().st_size
    if size > max_bytes:
        target.unlink(missing_ok=True)
        raise MediaDownloadError(
            f"حجم الملف بعد التنزيل تجاوز {settings.max_download_mb} MB. أرسل نسخة أصغر."
        )

    detected_suffix, detected_mime, kind = _Sniffer(target, original_name, declared_mime).detect()
    target = _with_detected_suffix(target, detected_suffix)
    final_name = target.name
    digest = _sha256(target)

    log.info(
        "[%s] %s stored as %s: mime=%s kind=%s bytes=%d sha256=%s",
        trace_id, telegram_kind, final_name, detected_mime, kind, size, digest[:12],
    )
    return MediaInfo(
        path=target,
        original_name=original_name,
        name=final_name,
        mime=detected_mime,
        kind=kind,
        size=size,
        sha256=digest,
        telegram_kind=telegram_kind,
    )


def sniff_local_file(path: Path, original_name: str = "", declared_mime: str | None = None) -> tuple[str, str, str]:
    """Expose detection for tests and diagnostics: (suffix, mime, kind)."""
    return _Sniffer(Path(path), original_name or Path(path).name, declared_mime).detect()
