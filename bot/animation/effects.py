"""Create Telegram-compatible short VP9 sticker loops from still images.

Each effect is an FFmpeg filter graph on a 512×512 RGBA canvas. Output must be
VP9 + alpha, ≤ 3 seconds and ≤ 256 KB, which is validated before the result is
handed back to the user.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

VIDEO_LIMIT = 256 * 1024
MAX_DURATION = 3.0
MIN_DURATION = 0.5

ALIASES = {
    "zoom": "zoom_in",
    "zoom_in": "zoom_in",
    "zoom_out": "zoom_out",
    "shake": "shake",
    "fade": "fade",
    "fade_in": "fade_in",
    "fade_out": "fade_out",
    "pulse": "pulse",
    "slide_left": "slide_left",
    "slide_right": "slide_right",
    "slide_up": "slide_up",
    "slide_down": "slide_down",
    "rotate": "rotate",
}

EFFECT_LABELS = {
    "zoom_in": "تكبير تدريجي",
    "zoom_out": "تصغير تدريجي",
    "shake": "اهتزاز",
    "fade": "ظهور واختفاء",
    "fade_in": "ظهور",
    "fade_out": "اختفاء",
    "pulse": "نبض",
    "slide_left": "انزلاق لليسار",
    "slide_right": "انزلاق لليمين",
    "slide_up": "انزلاق للأعلى",
    "slide_down": "انزلاق للأسفل",
    "rotate": "تمايل دوراني",
}


class AnimationError(ValueError):
    pass


def normalize_effect(effect: str) -> str:
    name = (effect or "").strip().lower()
    if name not in ALIASES:
        raise AnimationError("التأثير غير مدعوم.")
    return ALIASES[name]


def clamp_duration(duration: float) -> float:
    try:
        value = float(duration)
    except (TypeError, ValueError):
        raise AnimationError("مدة الحركة غير صالحة.") from None
    if value < MIN_DURATION or value > MAX_DURATION:
        raise AnimationError(
            f"مدة الحركة يجب أن تكون بين {MIN_DURATION} و{MAX_DURATION} ثانية (حد تيليجرام)."
        )
    return round(value, 2)


def clamp_speed(speed: float) -> float:
    try:
        value = float(speed)
    except (TypeError, ValueError):
        raise AnimationError("سرعة الحركة غير صالحة.") from None
    return round(max(0.25, min(4.0, value)), 2)


def build_filter(effect: str, duration: float = 3.0, speed: float = 1.0, fps: int = 30) -> str:
    """Return the FFmpeg filter graph for one effect (pure, unit-testable)."""
    name = normalize_effect(effect)
    duration = clamp_duration(duration)
    speed = clamp_speed(speed)
    fps = max(10, min(30, int(fps)))
    freq = round(speed, 2)
    margin = 48
    padded = 512 + margin * 2
    if name == "zoom_in":
        step = round(0.08 / max(1.0, duration * fps), 6)
        graph = f"zoompan=z='min(zoom+{step},1.10)':d=1:s=512x512:fps={fps}"
    elif name == "zoom_out":
        step = round(0.08 / max(1.0, duration * fps), 6)
        graph = (
            f"zoompan=z='if(lte(on\\,1)\\,1.10\\,max(zoom-{step}\\,1.0))'"
            f":d=1:s=512x512:fps={fps}"
        )
    elif name == "shake":
        graph = (
            f"scale=560:560:force_original_aspect_ratio=decrease,"
            f"pad=560:560:(ow-iw)/2:(oh-ih)/2,"
            f"crop=512:512:x='24+8*sin(2*PI*t*{3 * freq})':y='24+8*cos(2*PI*t*{3 * freq})',"
            f"fps={fps}"
        )
    elif name == "fade":
        tail = max(0.1, min(duration / 2, 0.45))
        graph = (
            f"fps={fps},fade=t=in:st=0:d={tail}:alpha=1,"
            f"fade=t=out:st={max(0.0, duration - tail):.2f}:d={tail}:alpha=1"
        )
    elif name == "fade_in":
        tail = max(0.1, min(duration / 2, 0.6))
        graph = f"fps={fps},fade=t=in:st=0:d={tail}:alpha=1"
    elif name == "fade_out":
        tail = max(0.1, min(duration / 2, 0.6))
        graph = f"fps={fps},fade=t=out:st={max(0.0, duration - tail):.2f}:d={tail}:alpha=1"
    elif name == "pulse":
        graph = (
            f"zoompan=z='1.03+0.035*sin(2*PI*on*{freq}/{fps})'"
            f":d=1:s=512x512:fps={fps}"
        )
    elif name.startswith("slide_"):
        horizontal = name in {"slide_left", "slide_right"}
        direction = 1 if name in {"slide_left", "slide_up"} else -1
        shift = f"{margin}*{direction}*(1-t/{duration:.2f})"
        if horizontal:
            graph = (
                f"pad={padded}:{padded}:{margin}:{margin}:color=black@0,"
                f"crop=512:512:x='{margin}+{shift}':y='{margin}',fps={fps}"
            )
        else:
            graph = (
                f"pad={padded}:{padded}:{margin}:{margin}:color=black@0,"
                f"crop=512:512:x='{margin}':y='{margin}+{shift}',fps={fps}"
            )
    elif name == "rotate":
        graph = (
            f"rotate=a='0.10*sin(2*PI*{freq}*t/{duration:.2f})'"
            f":c=none:ow=512:oh=512,fps={fps}"
        )
    else:  # pragma: no cover - normalize_effect already blocks this
        raise AnimationError("التأثير غير مدعوم.")
    return f"{graph},format=yuva420p"


def render_effect(
    source: Path,
    destination: Path,
    effect: str,
    ffmpeg: str = "ffmpeg",
    timeout: int = 120,
    ffprobe: str = "ffprobe",
    duration: float = 3.0,
    speed: float = 1.0,
    fps: int = 30,
) -> dict[str, Any]:
    filter_graph = build_filter(effect, duration, speed, fps)
    duration = clamp_duration(duration)
    fps = max(10, min(30, int(fps)))
    frame = destination.with_name(destination.stem + "_frame.png")
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            image.thumbnail((512, 512), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
            canvas.alpha_composite(image, ((512 - image.width) // 2, (512 - image.height) // 2))
            canvas.save(frame, format="PNG", optimize=True)
    except Exception as exc:
        raise AnimationError("تعذر تجهيز الصورة للتحريك.") from exc

    last_size = 0
    try:
        for crf in (38, 44, 50, 56, 62):
            destination.unlink(missing_ok=True)
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-loop", "1", "-framerate", str(fps), "-i", str(frame),
                "-t", f"{duration:.2f}",
                "-vf", filter_graph, "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-crf", str(crf), "-b:v", "0", "-deadline", "realtime", "-cpu-used", "8",
                "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1", str(destination),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise AnimationError("انتهت مهلة التحريك.") from exc
                continue
            last_size = destination.stat().st_size if destination.exists() else 0
            if 0 < last_size <= VIDEO_LIMIT:
                info = _verify(destination, ffprobe, timeout, duration, ffmpeg)
                return {
                    "format": "WEBM", "codec": "VP9", "width": 512, "height": 512,
                    "duration": info, "size": last_size, "effect": normalize_effect(effect),
                    "fps": fps, "alpha_plane": True, "audio": False,
                }
        destination.unlink(missing_ok=True)
        if last_size > VIDEO_LIMIT:
            raise AnimationError(
                f"حجم الحركة أكبر من 256KB (الحجم الأخير {last_size // 1024}KB). "
                "جرّب صورة أبسط أو مدة أقل."
            )
        raise AnimationError("لم يتمكن FFmpeg من إنشاء ملصق الفيديو. تأكد من دعم libvpx-vp9.")
    finally:
        frame.unlink(missing_ok=True)


def _verify(destination: Path, ffprobe: str, timeout: int, duration: float, ffmpeg: str) -> float:
    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=codec_name,width,height:format=duration",
             "-of", "json", str(destination)],
            check=True, capture_output=True, text=True, timeout=min(timeout, 15),
        )
        metadata = json.loads(probe.stdout)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise AnimationError("تعذر التحقق من فيديو WEBM الناتج؛ تأكد من تثبيت FFprobe.") from exc
    streams = [item for item in metadata.get("streams", []) if item.get("codec_name") == "vp9"]
    actual = float(metadata.get("format", {}).get("duration") or 0)
    if (not streams or int(streams[0].get("width") or 0) != 512
            or int(streams[0].get("height") or 0) != 512
            or actual <= 0 or actual > duration + 0.1):
        destination.unlink(missing_ok=True)
        raise AnimationError("ملف WEBM الناتج لا يطابق أبعاد أو مدة ملصقات تيليجرام.")
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-c:v",
             "libvpx-vp9", "-i", str(destination), "-vf", "alphaextract", "-frames:v", "1",
             "-f", "null", "-"],
            check=True, capture_output=True, text=True, timeout=min(timeout, 20),
        )
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise AnimationError("فشل التحقق من شفافية ملصق WEBM الناتج.") from exc
    return actual


def extract_frame(
    source: Path,
    destination: Path,
    at_seconds: float = 1.0,
    ffmpeg: str = "ffmpeg",
    timeout: int = 30,
) -> Path:
    """Save one frame as PNG so the user can preview an animation before sending it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, float(at_seconds)):.2f}", "-i", str(source),
        "-frames:v", "1", str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AnimationError("انتهت مهلة استخراج المعاينة.") from exc
    except Exception as exc:
        raise AnimationError("تعذر استخراج إطار المعاينة من الفيديو.") from exc
    if not destination.exists() or destination.stat().st_size == 0:
        raise AnimationError("لم يتم إنتاج إطار معاينة صالح.")
    return destination
