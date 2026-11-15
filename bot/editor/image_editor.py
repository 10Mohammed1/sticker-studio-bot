"""Memory-bounded, deterministic sticker image editing built on Pillow.

Every tool is a pure function on an RGBA image, which keeps the module testable
without Telegram and makes template replay (``apply_operations``) exact.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageFont

MAX_INPUT_PIXELS = 30_000_000
CANVAS = (512, 512)
MAX_TEXT = 80

TEXT_ACTIONS = {"text", "watermark"}
DEFAULT_WATERMARK = "© Sticker Studio"

ACTION_LABELS = {
    "crop": "قص مربع",
    "resize": "ضبط المقاس",
    "rotate": "تدوير",
    "flip_h": "قلب أفقي",
    "flip_v": "قلب رأسي",
    "outline": "حدود",
    "shadow": "ظل",
    "blur": "تمويه",
    "sharpen": "تحسين الحدة",
    "grayscale": "أبيض وأسود",
    "sepia": "سيبيا",
    "brightness": "سطوع",
    "contrast": "تباين",
    "saturation": "تشبع",
    "text": "نص",
    "watermark": "علامة مائية",
    "background": "خلفية ملونة",
    "crop_45": "قص 4:5",
    "crop_169": "قص 16:9",
}

COLORS = {
    "black": (0, 0, 0, 255),
    "white": (255, 255, 255, 255),
    "red": (220, 45, 45, 255),
    "green": (30, 160, 80, 255),
    "blue": (40, 110, 230, 255),
    "yellow": (245, 200, 40, 255),
    "transparent": (0, 0, 0, 0),
}


class EditorError(ValueError):
    """An expected image-editing error safe to show to the user."""


def _open_rgba(source: Path) -> Image.Image:
    try:
        with Image.open(source) as opened:
            if opened.width * opened.height > MAX_INPUT_PIXELS:
                raise EditorError("أبعاد الصورة كبيرة جدًا للتحرير (الحد 30 مليون بكسل).")
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            image.load()
            return image
    except EditorError:
        raise
    except Exception as exc:
        raise EditorError("تعذر فتح الصورة. أرسل صورة PNG أو JPG أو WEBP.") from exc


def _fit_canvas(
    image: Image.Image, *, fill: tuple[int, int, int, int] = (0, 0, 0, 0)
) -> Image.Image:
    image.thumbnail(CANVAS, Image.Resampling.LANCZOS)
    background = Image.new("RGBA", CANVAS, fill)
    background.alpha_composite(image, ((512 - image.width) // 2, (512 - image.height) // 2))
    return background


def _save(image: Image.Image, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    return destination


def _color(options: Mapping[str, Any], key: str = "color",
           default: tuple[int, int, int, int] = (255, 255, 255, 255)) -> tuple[int, int, int, int]:
    raw = str(options.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in COLORS:
        return COLORS[raw]
    if raw.startswith("#") and len(raw) in {7, 9}:
        try:
            values = [int(raw[index:index + 2], 16) for index in range(1, len(raw), 2)]
        except ValueError:
            raise EditorError("صيغة اللون غير صحيحة. استخدم اسمًا أو #RRGGBB.") from None
        if len(values) == 3:
            values.append(255)
        return tuple(values)  # type: ignore[return-value]
    raise EditorError("لون غير معروف. استخدم black/white/red/green/blue/yellow أو #RRGGBB.")


def _factor(options: Mapping[str, Any], default: float = 1.2) -> float:
    try:
        value = float(options.get("factor", default))
    except (TypeError, ValueError):
        value = default
    return max(0.2, min(3.0, value))


def _crop_ratio(image: Image.Image, ratio: float) -> Image.Image:
    width, height = image.size
    current = width / height if height else 1.0
    if current > ratio:
        new_width = int(height * ratio)
        left = (width - new_width) // 2
        return image.crop((left, 0, left + new_width, height))
    new_height = int(width / ratio)
    top = (height - new_height) // 2
    return image.crop((0, top, width, top + new_height))


def edit_image(
    source: Path,
    destination: Path,
    action: str,
    text: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> Path:
    image = _open_rgba(source)
    if action in TEXT_ACTIONS:
        image = _fit_canvas(image)
    image = _apply(image, action, text, options or {})
    image = _fit_canvas(image)
    return _save(image, destination)


def apply_operations(
    source: Path,
    destination: Path,
    operations: Iterable[Mapping[str, Any]],
) -> Path:
    """Replay a saved template: a list of ``{"action": ..., "text": ..., "options": ...}``."""
    image = _open_rgba(source)
    for operation in operations:
        action = str(operation.get("action") or "")
        if not action:
            continue
        if action in TEXT_ACTIONS:
            image = _fit_canvas(image)
        image = _apply(image, action, operation.get("text"), operation.get("options") or {})
    image = _fit_canvas(image)
    return _save(image, destination)


def _apply(
    image: Image.Image,
    action: str,
    text: str | None,
    options: Mapping[str, Any],
) -> Image.Image:
    if action == "crop":
        side = min(image.size)
        left = (image.width - side) // 2
        top = (image.height - side) // 2
        return image.crop((left, top, left + side, top + side))
    if action == "crop_45":
        return _crop_ratio(image, 4 / 5)
    if action == "crop_169":
        return _crop_ratio(image, 16 / 9)
    if action == "resize":
        return _fit_canvas(image)
    if action == "rotate":
        return image.rotate(90, expand=True, resample=Image.Resampling.BICUBIC)
    if action == "flip_h":
        return ImageOps.mirror(image)
    if action == "flip_v":
        return ImageOps.flip(image)
    if action == "outline":
        alpha = image.getchannel("A")
        expanded = alpha.filter(ImageFilter.MaxFilter(9))
        outline = Image.new("RGBA", image.size, _color(options, "color", (255, 255, 255, 255)))
        outline.putalpha(expanded)
        return Image.alpha_composite(outline, image)
    if action == "shadow":
        alpha = image.getchannel("A")
        shadow_alpha = alpha.filter(ImageFilter.GaussianBlur(10))
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        shadow.putalpha(shadow_alpha.point(lambda value: int(value * 0.65)))
        shadow.alpha_composite(image)
        return shadow
    if action == "blur":
        return image.filter(ImageFilter.GaussianBlur(radius=max(0.5, _factor(options, 2.2))))
    if action == "sharpen":
        return image.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    if action == "grayscale":
        gray = ImageOps.grayscale(image).convert("RGBA")
        gray.putalpha(image.getchannel("A"))
        return gray
    if action == "sepia":
        alpha = image.getchannel("A")
        rgb = ImageOps.grayscale(image.convert("RGB"))
        sepia = ImageOps.colorize(rgb, black="#4b321f", white="#f5e6c8").convert("RGBA")
        sepia.putalpha(alpha)
        return sepia
    if action == "brightness":
        return ImageEnhance.Brightness(image).enhance(_factor(options, 1.2))
    if action == "contrast":
        return ImageEnhance.Contrast(image).enhance(_factor(options, 1.25))
    if action == "saturation":
        alpha = image.getchannel("A")
        boosted = ImageEnhance.Color(image.convert("RGB")).enhance(_factor(options, 1.4))
        result = boosted.convert("RGBA")
        result.putalpha(alpha)
        return result
    if action == "background":
        color = _color(options, "color", (255, 255, 255, 255))
        # A chosen background must fill the whole sticker canvas, not just the
        # image rectangle, otherwise transparent margins survive.
        return _fit_canvas(image, fill=color)
    if action == "text":
        return _draw_text(image, text, options, default_size=34, default_position="bottom")
    if action == "watermark":
        value = (text or "").strip() or DEFAULT_WATERMARK
        return _draw_text(image, value, options, default_size=20, default_position="bottom-right")
    raise EditorError("أداة التحرير غير معروفة.")


def _draw_text(
    image: Image.Image,
    text: str | None,
    options: Mapping[str, Any],
    *,
    default_size: int,
    default_position: str,
) -> Image.Image:
    value = (text or "").strip()
    if not value:
        raise EditorError("النص فارغ.")
    if len(value) > MAX_TEXT:
        raise EditorError(f"النص طويل جدًا؛ الحد {MAX_TEXT} حرفًا.")
    try:
        size = int(options.get("size", default_size))
    except (TypeError, ValueError):
        size = default_size
    size = max(12, min(120, size))
    position = str(options.get("position") or default_position).lower()
    fill = _color(options, "color", (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = _load_font(size)
    lines = value.splitlines()[:3]
    boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
    heights = [box[3] - box[1] for box in boxes]
    gap = max(2, size // 6)
    total = sum(heights) + gap * (len(lines) - 1)
    margin = 14
    if "top" in position:
        y = margin
    elif "middle" in position or position == "center":
        y = max(margin, (512 - total) // 2)
    else:
        y = max(margin, 512 - total - margin)
    for line, box, height in zip(lines, boxes, heights):
        width = box[2] - box[0]
        if "right" in position:
            x = 512 - width - margin
        elif "left" in position:
            x = margin
        else:
            x = max(margin, (512 - width) // 2)
        draw.text(
            (x, y), line, font=font, fill=fill,
            stroke_width=3 if size >= 26 else 2, stroke_fill=(0, 0, 0, 200),
        )
        y += height + gap
    return image


def _load_font(size: int):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()
