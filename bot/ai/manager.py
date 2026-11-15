"""Capability reporting and runtime switches for the optional AI stack.

The bot must stay usable when nothing extra is installed, so every lookup here
degrades to ``False`` instead of raising. The admin panel can flip background
removal at runtime without a restart by storing the choice in ``settings``.
"""
from __future__ import annotations

import importlib.util
from typing import Any

from ..config import settings

BACKGROUND_KEY = "ai_background"

CAPABILITY_LABELS = {
    "rembg": "إزالة الخلفية (rembg)",
    "cv2": "اكتشاف الوجوه والعناصر (OpenCV)",
    "onnxruntime": "نماذج ONNX المتقدمة",
    "numpy": "معالجة مصفوفات سريعة (numpy)",
}


def installed() -> dict[str, bool]:
    """Which optional packages are importable right now."""
    return {
        name: importlib.util.find_spec(name) is not None
        for name in CAPABILITY_LABELS
    }


def light_available() -> bool:
    """Light AI only needs Pillow, which is a hard dependency."""
    return importlib.util.find_spec("PIL") is not None


async def background_enabled(db: Any) -> bool:
    """Environment default, overridable from the admin panel."""
    if not settings.enable_bg_removal:
        override = await db.get_setting(BACKGROUND_KEY)
        return override == "1"
    override = await db.get_setting(BACKGROUND_KEY)
    return True if override is None else override == "1"


async def set_background(db: Any, enabled: bool) -> None:
    await db.set_setting(BACKGROUND_KEY, "1" if enabled else "0")


def capabilities_report() -> str:
    """Arabic summary of what actually works on this host."""
    found = installed()
    lines = ["🧠 <b>حالة الذكاء الاصطناعي</b>"]
    lines.append(
        f"• Light AI (تحليل/قص/تحسين): {'✅ متاح دائمًا' if light_available() else '❌ غير متاح'}"
    )
    for name, label in CAPABILITY_LABELS.items():
        state = "✅ مثبت" if found.get(name) else "➖ غير مثبت"
        lines.append(f"• {label}: {state}")
    if not found.get("rembg"):
        lines.append(
            "\nإزالة الخلفية تحتاج <code>pip install -r requirements-ai.txt</code> "
            "وتُعطَّل تلقائيًا على الاستضافات الضعيفة."
        )
    lines.append(
        "\nLight AI يعمل دون نماذج كبيرة: تحليل الصورة، قص تلقائي، تحسين الجودة، تكبير ذكي."
    )
    return "\n".join(lines)


__all__ = [
    "BACKGROUND_KEY",
    "CAPABILITY_LABELS",
    "background_enabled",
    "capabilities_report",
    "installed",
    "light_available",
    "set_background",
]
