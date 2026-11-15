"""Lazy optional background removal with rembg CPU support."""
from __future__ import annotations

from pathlib import Path
from threading import Lock
from PIL import Image

_session = None
_session_lock = Lock()


class AIUnavailable(RuntimeError):
    pass


def remove_background(source: Path, destination: Path) -> Path:
    """Remove a background. rembg is imported lazily and model is cached per process."""
    global _session
    try:
        from rembg import new_session, remove
    except Exception as exc:
        raise AIUnavailable(
            "ميزة إزالة الخلفية غير مثبّتة. أضف requirements-ai.txt إلى بيئة التشغيل."
        ) from exc
    with _session_lock:
        if _session is None:
            try:
                _session = new_session("u2netp")
            except Exception as exc:
                raise AIUnavailable("تعذر تحميل نموذج إزالة الخلفية المحلي.") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as image:
            result = remove(image.convert("RGBA"), session=_session)
            if isinstance(result, bytes):
                from io import BytesIO
                result = Image.open(BytesIO(result)).convert("RGBA")
            result.save(destination, format="PNG", optimize=True)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise AIUnavailable("فشلت إزالة الخلفية لهذه الصورة.") from exc
    return destination
