"""Light AI tools that need no large models and no network.

Everything here is deterministic Pillow work, so it runs on the weakest host and
never blocks the bot if a heavy model is missing. Advanced models (rembg,
OpenCV, ONNX) stay optional and are reported by :mod:`bot.ai.manager`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps, ImageStat

CANVAS = (512, 512)
MAX_UPSCALE = 2048
TOLERANCE = 12


class LightAIError(ValueError):
    """A user-facing explanation of why a light-AI step could not run."""


def _open(source: Path) -> Image.Image:
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            image.load()
            return image
    except Exception as exc:  # noqa: BLE001
        raise LightAIError("تعذر فتح الصورة لتحليلها.") from exc


def _save(image: Image.Image, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    return destination


def _background_colour(image: Image.Image) -> tuple[int, int, int]:
    """Most common corner colour: a robust guess for the background."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    corners = [
        rgb.getpixel((0, 0)), rgb.getpixel((width - 1, 0)),
        rgb.getpixel((0, height - 1)), rgb.getpixel((width - 1, height - 1)),
    ]
    clustered: dict[tuple[int, int, int], int] = {}
    for colour in corners:
        key = tuple(round(channel / 32) * 32 for channel in colour)
        clustered[key] = clustered.get(key, 0) + 1
    best = max(clustered, key=clustered.get)
    return best  # type: ignore[return-value]


def content_bbox(image: Image.Image, tolerance: int = TOLERANCE) -> tuple[int, int, int, int]:
    """Bounding box of the meaningful content, ignoring empty margins.

    Transparency wins when present; otherwise pixels close to the corner colour
    are treated as background. Falls back to the full image.
    """
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha.getextrema()[0] < 255:
        mask = alpha.point(lambda value: 255 if value > 8 else 0)
        box = mask.getbbox()
        if box:
            return box
    rgb = rgba.convert("RGB")
    background = _background_colour(rgb)
    difference = ImageChops.difference(rgb, Image.new("RGB", rgb.size, background))
    mask = difference.convert("L").point(lambda value: 255 if value > tolerance else 0)
    return mask.getbbox() or (0, 0, rgba.width, rgba.height)


def trim_margins(image: Image.Image, padding: int = 0) -> Image.Image:
    """Crop away empty margins while keeping a small safety padding."""
    box = content_bbox(image)
    left, top, right, bottom = box
    width, height = image.size
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(width, right + padding)
    bottom = min(height, bottom + padding)
    if right - left < 8 or bottom - top < 8:
        return image.copy()
    return image.crop((left, top, right, bottom))


def frame_on_canvas(
    image: Image.Image,
    size: tuple[int, int] = CANVAS,
    *,
    margin_ratio: float = 0.06,
    fill: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> Image.Image:
    """Centre the image on a Telegram canvas with even transparent margins."""
    margin = int(min(size) * max(0.0, min(0.25, margin_ratio)))
    inner = (max(16, size[0] - margin * 2), max(16, size[1] - margin * 2))
    scaled = image.copy()
    scaled.thumbnail(inner, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", size, fill)
    canvas.alpha_composite(
        scaled, ((size[0] - scaled.width) // 2, (size[1] - scaled.height) // 2)
    )
    return canvas


def auto_frame(image: Image.Image) -> Image.Image:
    """Auto-crop the subject then centre it: the "smart crop" suggestion."""
    return frame_on_canvas(trim_margins(image))


def enhance(image: Image.Image) -> Image.Image:
    """One-click quality pass: exposure, contrast and a light sharpen."""
    rgb = image.convert("RGBA")
    alpha = rgb.getchannel("A")
    enhanced = ImageOps.autocontrast(rgb.convert("RGB"), cutoff=1)
    enhanced = ImageEnhance.Contrast(enhanced).enhance(1.06)
    enhanced = ImageEnhance.Color(enhanced).enhance(1.08)
    enhanced = enhanced.filter(ImageFilter.UnsharpMask(radius=1.6, percent=110, threshold=3))
    result = enhanced.convert("RGBA")
    result.putalpha(alpha)
    return frame_on_canvas(result)


def upscale(image: Image.Image, factor: float = 2.0) -> Image.Image:
    """Bounded LANCZOS upscale with a sharpening pass to hide softness."""
    factor = max(1.1, min(4.0, float(factor)))
    target = (
        min(MAX_UPSCALE, max(8, int(image.width * factor))),
        min(MAX_UPSCALE, max(8, int(image.height * factor))),
    )
    enlarged = image.resize(target, Image.Resampling.LANCZOS)
    sharpened = enlarged.filter(ImageFilter.UnsharpMask(radius=1.4, percent=90, threshold=2))
    return frame_on_canvas(sharpened)


def _flatten(image: Image.Image) -> Image.Image:
    """Composite transparency over white so colours/brightness read correctly."""
    rgba = image.convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] < 255:
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        canvas.alpha_composite(rgba)
        return canvas
    return rgba


def dominant_colours(image: Image.Image, count: int = 4) -> list[str]:
    """Return the main palette as ``#RRGGBB`` strings (largest area first)."""
    flat = _flatten(image)
    quantized = flat.convert("RGB").quantize(
        colors=max(2, min(16, count)), method=Image.Quantize.MEDIANCUT
    )
    palette = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], reverse=True)
    colours: list[str] = []
    for _, index in counts[:count]:
        base = index * 3
        if base + 2 >= len(palette):
            continue
        r, g, b = palette[base:base + 3]
        colours.append(f"#{r:02X}{g:02X}{b:02X}")
    return colours


def analyze(image: Image.Image) -> dict[str, Any]:
    """Metadata shown to the user before they pick an automatic fix."""
    rgba = image.convert("RGBA")
    brightness = round(ImageStat.Stat(_flatten(rgba).convert("L")).mean[0], 1)
    box = content_bbox(rgba)
    coverage = round(
        ((box[2] - box[0]) * (box[3] - box[1])) / max(1, rgba.width * rgba.height) * 100, 1
    )
    transparent = rgba.getchannel("A").getextrema()[0] < 255
    suggestions: list[str] = []
    if coverage < 55:
        suggestions.append("قص_تلقائي")
    if brightness < 70:
        suggestions.append("تحسين_الإضاءة")
    if brightness > 220:
        suggestions.append("تقليل_السطوع")
    if rgba.width < 512 or rgba.height < 512:
        suggestions.append("تكبير_ذكي")
    if transparent:
        suggestions.append("الحفاظ_على_الشفافية")
    return {
        "width": rgba.width,
        "height": rgba.height,
        "brightness": brightness,
        "transparent": transparent,
        "content_coverage": coverage,
        "colours": dominant_colours(rgba),
        "suggestions": suggestions,
        "crop_box": box,
    }


def describe(image: Image.Image) -> str:
    """Arabic one-paragraph analysis for the editor panel."""
    data = analyze(image)
    lines = [
        "🧠 <b>تحليل الصورة (Light AI)</b>",
        f"📐 الأبعاد: {data['width']}×{data['height']}",
        f"💡 السطوع: {data['brightness']}/255",
        f"🎯 نسبة المحتوى: {data['content_coverage']}%",
        f"🎨 الألوان الأساسية: {' '.join(data['colours']) or '—'}",
        f"🫥 شفافية: {'نعم' if data['transparent'] else 'لا'}",
    ]
    if data["suggestions"]:
        lines.append(f"✅ مقترحات: {'، '.join(data['suggestions'])}")
    else:
        lines.append("✅ الصورة متوازنة؛ لا حاجة إلى تصحيح تلقائي.")
    return "\n".join(lines)


def describe_file(source: Path) -> str:
    """Analysis report for a file on disk (safe to call from a worker thread)."""
    return describe(_open(source))


def run_light_tool(source: Path, destination: Path, action: str) -> Path:
    """Apply one named light-AI action and write a 512 PNG."""
    image = _open(source)
    if action == "auto":
        result = auto_frame(image)
    elif action == "enhance":
        result = enhance(image)
    elif action == "upscale":
        result = upscale(image, 2.0)
    elif action == "trim":
        result = frame_on_canvas(trim_margins(image))
    else:
        raise LightAIError("أداة الذكاء الخفيف غير معروفة.")
    return _save(result, destination)


__all__ = [
    "LightAIError",
    "analyze",
    "auto_frame",
    "content_bbox",
    "describe",
    "describe_file",
    "dominant_colours",
    "enhance",
    "frame_on_canvas",
    "run_light_tool",
    "trim_margins",
    "upscale",
]
