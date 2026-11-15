"""Safe file-to-Telegram-sticker conversion helpers.

Static output: transparent-capable WEBP, 512px on the longest side, <=512 KiB.
Video output: VP9 WEBM, no audio, <=3 seconds, <=30 FPS, <=256 KiB.
TGS files are validated and passed through; raster video cannot be truthfully
converted to vector Lottie/TGS by changing its extension.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, ImageFile, ImageFilter, ImageOps, UnidentifiedImageError

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover - optional dependency
    fitz = None

try:
    import resvg_py
except Exception:  # pragma: no cover - optional dependency
    resvg_py = None

try:
    import cairosvg  # legacy fallback for existing installations
except Exception:  # pragma: no cover - optional dependency
    cairosvg = None

STATIC_LIMIT = 512 * 1024
VIDEO_LIMIT = 256 * 1024
MAX_SIDE = 512
VIDEO_SECONDS = 3.0
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".jfif"}
ANIM_EXTS = {".gif", ".apng"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mpeg", ".mpg", ".3gp"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac", ".wma", ".amr"}
VECTOR_EXTS = {".svg"}
DOC_MEDIA_EXTS = IMAGE_EXTS | ANIM_EXTS | VIDEO_EXTS | AUDIO_EXTS | VECTOR_EXTS | {".pdf", ".tgs"}
ARCHIVE_EXTS = {".zip", ".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods"}


class ConversionError(Exception):
    """An expected, user-friendly conversion failure."""


@dataclass
class OutputFile:
    path: Path
    kind: str
    note: str


def _safe_name(name: str, default: str = "converted") -> str:
    base = Path(name).name
    base = re.sub(r"[^\w.()\- ]+", "_", base, flags=re.UNICODE).strip(" .")
    return base[:100] or default


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("التحويل استغرق وقتًا أطول من المسموح. جرّب ملفًا أقصر أو أصغر.") from exc
    except OSError as exc:
        raise ConversionError(f"تعذّر تشغيل أداة التحويل: {exc}") from exc
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        detail = "\n".join(lines[-3:])[:500]
        raise ConversionError("فشل فك أو تحويل الملف. قد يكون تالفًا أو غير مدعوم.\n" + detail)
    return result


def _require_binary(binary: str) -> str:
    found = shutil.which(binary)
    if not found:
        raise ConversionError(f"الأداة {binary} غير مثبتة. ثبّت FFmpeg ثم أعد التشغيل.")
    return found


def _fit_size(width: int, height: int, side: int = MAX_SIDE) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ConversionError("أبعاد الصورة غير صالحة.")
    scale = side / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


@lru_cache(maxsize=1)
def _get_rembg_session():
    from rembg import new_session  # type: ignore
    # u2netp is a smaller model suitable for low-memory machines.
    return new_session("u2netp")


def _remove_background(image: Image.Image, enabled: bool) -> tuple[Image.Image, bool]:
    """Try local rembg segmentation; never fail conversion if optional AI is absent."""
    if not enabled or image.getchannel("A").getextrema()[0] < 255:
        return image, False
    try:
        from rembg import remove  # type: ignore
        result = remove(image, session=_get_rembg_session())
        if isinstance(result, Image.Image):
            return result.convert("RGBA"), True
        if isinstance(result, (bytes, bytearray)):
            import io
            return Image.open(io.BytesIO(result)).convert("RGBA"), True
    except Exception:
        # Optional model may be unavailable, not downloaded, or unable to segment.
        return image, False
    return image, False


def _sticker_canvas(image: Image.Image) -> tuple[Image.Image, bool]:
    """Trim transparent margins and add white stroke + soft shadow where possible."""
    image = image.convert("RGBA")
    alpha = image.getchannel("A")
    has_transparency = alpha.getextrema()[0] < 255
    bbox = alpha.getbbox() if has_transparency else None
    if has_transparency and bbox:
        image = image.crop(bbox)
        # Keep breathing room for the white stroke and shadow.
        target = _fit_size(*image.size, side=438)
        image = image.resize(target, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        x, y = (512 - image.width) // 2, (512 - image.height) // 2
        mask = image.getchannel("A")

        # Soft shadow behind the white outline.
        expanded_shadow = mask.filter(ImageFilter.MaxFilter(13))
        shadow = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        shadow_mask = Image.new("L", (512, 512), 0)
        shadow_mask.paste(expanded_shadow, (x + 2, y + 4))
        shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(4))
        shadow_mask = shadow_mask.point(lambda p: int(p * 0.48))
        shadow.putalpha(shadow_mask)
        canvas.alpha_composite(shadow)

        # White outer line.
        outline_mask = Image.new("L", (512, 512), 0)
        outline_mask.paste(mask.filter(ImageFilter.MaxFilter(9)), (x, y))
        outline = Image.new("RGBA", (512, 512), (255, 255, 255, 0))
        outline.putalpha(outline_mask)
        canvas.alpha_composite(outline)

        subject = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        subject.alpha_composite(image, (x, y))
        canvas.alpha_composite(subject)
        return canvas, True

    # An opaque photo has no separable subject. Keep the whole photo intact rather
    # than inventing a bad cut-out; output still has an alpha-capable RGBA plane.
    target = _fit_size(*image.size)
    image = image.resize(target, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    canvas.alpha_composite(image, ((512 - image.width) // 2, (512 - image.height) // 2))
    return canvas, False


def convert_image_to_webp(source: Path, destination: Path, enable_bg_removal: bool = True) -> dict[str, Any]:
    """Repair/read a raster image and create a Telegram-ready WEBP sticker file."""
    old_flag = ImageFile.LOAD_TRUNCATED_IMAGES
    try:
        try:
            with Image.open(source) as opened:
                opened.seek(0)
                if opened.width * opened.height > 25_000_000:
                    raise ConversionError("أبعاد الصورة ضخمة جدًا للمعالجة الآمنة.") from None
                opened.load()
                image = ImageOps.exif_transpose(opened.copy()).convert("RGBA")
        except (UnidentifiedImageError, OSError, ValueError):
            # One recovery attempt for partially truncated images.
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            with Image.open(source) as opened:
                opened.seek(0)
                if opened.width * opened.height > 25_000_000:
                    raise ConversionError("أبعاد الصورة ضخمة جدًا للمعالجة الآمنة.") from None
                image = ImageOps.exif_transpose(opened.copy()).convert("RGBA")
    except ConversionError:
        # Keep the specific, user-facing reason (for example oversized pixels).
        raise
    except Exception as exc:
        raise ConversionError("لم أستطع قراءة الصورة أو إصلاحها. قد يكون الملف تالفًا بالكامل.") from exc
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = old_flag

    image, bg_removed = _remove_background(image, enable_bg_removal)
    sticker, has_subject_alpha = _sticker_canvas(image)

    destination.parent.mkdir(parents=True, exist_ok=True)
    last_size = None
    # Progressive quality reduction; keep the dimensions required by Telegram.
    # method=4 is dramatically faster than method=6 on weak hosting CPUs
    # (milliseconds instead of many seconds) and produces only a few percent
    # larger files. The quality loop below still enforces Telegram's 512 KB cap.
    for quality in (92, 86, 80, 74, 68, 62, 56, 50, 44, 38, 32, 26):
        destination.unlink(missing_ok=True)
        sticker.save(destination, format="WEBP", quality=quality, method=4, exact=True)
        last_size = destination.stat().st_size
        if last_size <= STATIC_LIMIT:
            break
    if not last_size or last_size > STATIC_LIMIT:
        destination.unlink(missing_ok=True)
        raise ConversionError("الصورة معقدة جدًا ولم أستطع ضغطها إلى 512 KB دون إفسادها.")

    with Image.open(destination) as check:
        if check.format != "WEBP" or check.size != (512, 512) or check.mode not in {"RGBA", "RGB"}:
            destination.unlink(missing_ok=True)
            raise ConversionError("فشل فحص ملف WEBP الناتج.")
    return {
        "format": "WEBP", "width": 512, "height": 512,
        "size": destination.stat().st_size, "limit": STATIC_LIMIT,
        "background_removed": bg_removed,
        "subject_outline": has_subject_alpha,
    }


def _probe_video(source: Path, ffprobe: str, timeout: int) -> dict[str, Any]:
    result = _run([
        ffprobe, "-v", "error", "-show_entries",
        "format=duration:stream=codec_name,codec_type,width,height,pix_fmt,r_frame_rate,avg_frame_rate",
        "-of", "json", str(source)
    ], timeout)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError("تعذّر فحص الفيديو.") from exc


def _video_filter(fps: int) -> str:
    return (
        f"fps={fps},"
        "scale=w='if(gte(iw,ih),512,-2)':h='if(gte(iw,ih),-2,512)':flags=lanczos,"
        "setsar=1,format=yuva420p"
    )


def convert_video_to_webm(
    source: Path,
    destination: Path,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    timeout: int = 150,
) -> dict[str, Any]:
    """Create a valid short VP9 WebM video sticker, with no audio."""
    ffmpeg = _require_binary(ffmpeg_path)
    ffprobe = _require_binary(ffprobe_path)
    source_info = _probe_video(source, ffprobe, timeout)
    vstream = next((s for s in source_info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not vstream:
        raise ConversionError("الملف لا يحتوي على مسار فيديو.")
    duration_in = float(source_info.get("format", {}).get("duration") or 0)
    if duration_in <= 0:
        raise ConversionError("مدة الفيديو غير صالحة.")

    last_size = None
    chosen_duration, chosen_fps, chosen_crf = None, None, None
    deadline = time.monotonic() + timeout
    # Longer clips are trimmed automatically; noisy clips receive stronger compression.
    attempts = [(3.0, 30), (2.5, 30), (2.0, 24), (1.5, 20)]
    for clip_len, fps in attempts:
        for crf in (34, 38, 42, 46, 50, 54, 58, 62):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            destination.unlink(missing_ok=True)
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
                "-i", str(source), "-t", f"{min(clip_len, VIDEO_SECONDS):.2f}",
                "-map", "0:v:0", "-an", "-sn", "-dn",
                "-vf", _video_filter(fps),
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-crf", str(crf), "-b:v", "0", "-deadline", "good",
                "-cpu-used", "6", "-row-mt", "1", "-auto-alt-ref", "0",
                "-metadata:s:v:0", "alpha_mode=1", "-threads", "2", str(destination)
            ]
            try:
                _run(cmd, min(20, max(3, remaining)))
            except ConversionError:
                continue
            if destination.exists() and destination.stat().st_size:
                last_size = destination.stat().st_size
                if last_size <= VIDEO_LIMIT:
                    chosen_duration, chosen_fps, chosen_crf = clip_len, fps, crf
                    break
        if chosen_duration is not None or time.monotonic() >= deadline:
            break

    if chosen_duration is None:
        if time.monotonic() >= deadline:
            raise ConversionError("انتهت مهلة تحويل الفيديو. جرّب مقطعًا أقصر أو أقل دقة.")
        destination.unlink(missing_ok=True)
        extra = f" آخر حجم وصلنا إليه {last_size // 1024} KB." if last_size else ""
        raise ConversionError("حركة الفيديو معقدة جدًا ولا يمكن ضغطها إلى 256 KB بالجودة المقبولة." + extra)

    result_info = _probe_video(destination, ffprobe, min(30, max(3, deadline - time.monotonic())))
    streams = result_info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not videos or audios or videos[0].get("codec_name") != "vp9":
        destination.unlink(missing_ok=True)
        raise ConversionError("فشل فحص فيديو WEBM الناتج.")
    width = int(videos[0].get("width") or 0)
    height = int(videos[0].get("height") or 0)
    duration = float(result_info.get("format", {}).get("duration") or 0)
    if max(width, height) != 512 or duration <= 0 or duration > 3.05:
        destination.unlink(missing_ok=True)
        raise ConversionError("أبعاد أو مدة فيديو WEBM لا تطابق حدود ملصقات تيليجرام.")

    # Validate that the resulting VP9 stream exposes an alpha plane.
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-c:v", "libvpx-vp9",
        "-i", str(destination), "-vf", "alphaextract", "-frames:v", "1", "-f", "null", "-"
    ], min(20, max(3, deadline - time.monotonic())))
    if destination.stat().st_size > VIDEO_LIMIT:
        destination.unlink(missing_ok=True)
        raise ConversionError("حجم الفيديو الناتج تجاوز 256 KB.")
    return {
        "format": "WEBM", "codec": "VP9", "width": width, "height": height,
        "duration": duration, "fps_target": chosen_fps, "crf": chosen_crf,
        "size": destination.stat().st_size, "limit": VIDEO_LIMIT,
        "alpha_plane": True, "audio": False,
    }


def validate_webp_sticker(source: Path) -> dict[str, Any]:
    """Validate an existing Telegram static sticker without re-processing it."""
    size = source.stat().st_size
    if size <= 0:
        raise ConversionError("ملف WEBP فارغ.")
    if size > STATIC_LIMIT:
        raise ConversionError(
            f"ملصق WEBP أكبر من حد تيليجرام 512 KB ({size // 1024} KB)."
        )
    try:
        with Image.open(source) as opened:
            image_format = (opened.format or "").upper()
            width, height = opened.size
            frames = getattr(opened, "n_frames", 1)
    except Exception as exc:
        raise ConversionError("ملف WEBP غير صالح.") from exc
    if image_format != "WEBP":
        raise ConversionError("الملف ليس WEBP صالحًا.")
    if (width, height) != (MAX_SIDE, MAX_SIDE):
        raise ConversionError("ملصق WEBP يجب أن يكون 512×512 بالضبط.")
    if frames > 1:
        raise ConversionError("ملصق WEBP المتحرك غير مدعوم في تيليجرام.")
    return {"format": "WEBP", "size": size, "limit": STATIC_LIMIT,
            "width": width, "height": height}


def validate_webm_sticker(
    source: Path, ffprobe_path: str = "ffprobe", timeout: int = 30
) -> dict[str, Any]:
    """Accept an existing Telegram video sticker without re-encoding it.

    The Bot API only delivers files it already validated, so re-encoding a
    received sticker wastes CPU, needs FFmpeg, and loses alpha quality. Layout
    checks still run; FFprobe refines them when it happens to be installed.
    """
    size = source.stat().st_size
    if size <= 0:
        raise ConversionError("ملف WEBM فارغ.")
    if size > VIDEO_LIMIT:
        raise ConversionError(
            f"ملصق الفيديو أكبر من حد تيليجرام 256 KB ({size // 1024} KB). "
            "أرسله كفيديو عادي بدل ملصق ليُعاد ضغطه."
        )
    with source.open("rb") as handle:
        head = handle.read(64 * 1024)
    if not head.startswith(b"\x1a\x45\xdf\xa3"):
        raise ConversionError("الملف ليس WEBM صالحًا.")
    if b"V_VP9" not in head:
        raise ConversionError("ملصق الفيديو يجب أن يكون بترميز VP9.")
    if any(tag in head for tag in (b"A_OPUS", b"A_VORBIS", b"A_AAC", b"A_MPEG")):
        raise ConversionError("ملصق الفيديو يجب ألا يحتوي على مسار صوتي.")

    info: dict[str, Any] = {
        "format": "WEBM", "size": size, "limit": VIDEO_LIMIT, "probed": False,
        "width": None, "height": None, "duration": None,
    }
    try:
        ffprobe = _require_binary(ffprobe_path)
    except ConversionError:
        return info

    try:
        probed = _probe_video(source, ffprobe, timeout)
    except ConversionError:
        # FFprobe is optional here; the structural checks above already passed.
        return info
    streams = probed.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not videos or audios or videos[0].get("codec_name") != "vp9":
        raise ConversionError("بنية ملصق الفيديو غير صالحة (VP9 بدون صوت مطلوب).")
    width = int(videos[0].get("width") or 0)
    height = int(videos[0].get("height") or 0)
    duration = float(probed.get("format", {}).get("duration") or 0)
    if max(width, height) != MAX_SIDE:
        raise ConversionError("ملصق الفيديو يجب أن يكون أقصى ضلع فيه 512 بكسل.")
    if duration <= 0 or duration > VIDEO_SECONDS + 0.05:
        raise ConversionError("ملصق الفيديو يجب ألا تتجاوز مدته 3 ثوانٍ.")
    info.update(width=width, height=height, duration=duration, probed=True)
    return info


def convert_audio_to_webm(
    source: Path, destination: Path, ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe", timeout: int = 120,
) -> dict[str, Any]:
    """Turn an audio clip into a visual waveform video sticker (no audio track)."""
    ffmpeg = _require_binary(ffmpeg_path)
    ffprobe = _require_binary(ffprobe_path)
    meta = _probe_video(source, ffprobe, timeout)
    audio_stream = next((s for s in meta.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not audio_stream:
        raise ConversionError("الملف لا يحتوي على مسار صوتي يمكن تحويله إلى موجة مرئية.")

    last_size = None
    deadline = time.monotonic() + timeout
    for crf in (36, 40, 44, 48, 52, 56, 60, 63):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        destination.unlink(missing_ok=True)
        filt = (
            "[0:a]apad=pad_dur=3,atrim=0:3,aformat=channel_layouts=mono,"
            "showwaves=s=512x256:mode=line:rate=30:colors=white,format=rgba[w];"
            "color=c=0x20242C:s=512x512:r=30:d=3[bg];"
            "[bg][w]overlay=(W-w)/2:(H-h)/2,format=yuva420p[v]"
        )
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-filter_complex", filt, "-map", "[v]", "-an", "-t", "3",
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-crf", str(crf),
            "-b:v", "0", "-deadline", "good", "-cpu-used", "6", "-row-mt", "1",
            "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1", str(destination)
        ]
        try:
            _run(command, min(20, max(3, remaining)))
        except ConversionError:
            continue
        if destination.exists():
            last_size = destination.stat().st_size
            if 0 < last_size <= VIDEO_LIMIT:
                _run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-c:v", "libvpx-vp9",
                    "-i", str(destination), "-vf", "alphaextract", "-frames:v", "1", "-f", "null", "-"
                ], min(20, max(3, deadline - time.monotonic())))
                return {"format": "WEBM", "codec": "VP9", "width": 512, "height": 512,
                        "duration": 3.0, "size": last_size, "limit": VIDEO_LIMIT,
                        "visualization": "waveform"}
    destination.unlink(missing_ok=True)
    last = f" الحجم الأخير {last_size // 1024} KB." if last_size else ""
    raise ConversionError("لم أستطع ضغط موجة الصوت إلى 256 KB." + last)


def extract_video_frame(source: Path, destination: Path, ffmpeg_path: str = "ffmpeg", timestamp: float = 0.3, timeout: int = 60) -> Path:
    ffmpeg = _require_binary(ffmpeg_path)
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(max(0, timestamp)),
        "-i", str(source), "-frames:v", "1", str(destination)
    ], timeout)
    if not destination.exists() or destination.stat().st_size == 0:
        raise ConversionError("لم أستطع استخراج صورة من الفيديو.")
    return destination


def convert_image_to_motion_webm(
    source: Path, destination: Path, ffmpeg_path: str = "ffmpeg",
    timeout: int = 120, ffprobe_path: str = "ffprobe",
) -> dict[str, Any]:
    """Create a subtle 2-second zoom loop from a still image (optional /motion command)."""
    ffmpeg = _require_binary(ffmpeg_path)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened.copy()).convert("RGBA")
        fitted = _fit_size(*image.size)
        image = image.resize(fitted, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        canvas.alpha_composite(image, ((512 - fitted[0]) // 2, (512 - fitted[1]) // 2))
        frame = destination.with_name(destination.stem + "_frame.png")
        canvas.save(frame, format="PNG", optimize=True)
    try:
        last_size = None
        deadline = time.monotonic() + timeout
        for crf in (40, 46, 52, 58, 62):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            destination.unlink(missing_ok=True)
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-loop", "1", "-framerate", "30", "-i", str(frame), "-t", "2.0",
                "-vf", "zoompan=z='min(zoom+0.0008,1.04)':d=1:s=512x512:fps=30,format=yuva420p",
                "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-crf", str(crf),
                "-b:v", "0", "-deadline", "good", "-cpu-used", "6", "-auto-alt-ref", "0",
                "-metadata:s:v:0", "alpha_mode=1", str(destination)
            ]
            try:
                _run(cmd, min(20, max(3, remaining)))
            except ConversionError:
                continue
            last_size = destination.stat().st_size if destination.exists() else 0
            if last_size and last_size <= VIDEO_LIMIT:
                checked = _probe_video(destination, _require_binary(ffprobe_path), min(20, max(3, deadline - time.monotonic())))
                streams = checked.get("streams", [])
                videos = [item for item in streams if item.get("codec_type") == "video"]
                audios = [item for item in streams if item.get("codec_type") == "audio"]
                duration = float(checked.get("format", {}).get("duration") or 0)
                if not videos or audios or videos[0].get("codec_name") != "vp9" or max(int(videos[0].get("width") or 0), int(videos[0].get("height") or 0)) != 512 or not (0 < duration <= 3.0):
                    raise ConversionError("فشل فحص ملصق الفيديو الناتج من الصورة.")
                _run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-c:v", "libvpx-vp9",
                    "-i", str(destination), "-vf", "alphaextract", "-frames:v", "1", "-f", "null", "-"
                ], min(20, max(3, deadline - time.monotonic())))
                return {"format": "WEBM", "codec": "VP9", "width": 512, "height": 512, "duration": duration, "size": last_size}
        destination.unlink(missing_ok=True)
        if time.monotonic() >= deadline:
            raise ConversionError("انتهت مهلة تحويل الصورة إلى حركة.")
        raise ConversionError(f"لم أستطع ضغط الحركة الناتجة إلى 256 KB. الحجم الأخير: {last_size // 1024 if last_size else 0} KB.")
    finally:
        frame.unlink(missing_ok=True)


def _sanitize_svg(svg_bytes: bytes) -> bytes:
    """Remove active content, external resources, and XML declarations/entities."""
    if len(svg_bytes) > 5 * 1024 * 1024:
        raise ConversionError("ملف SVG أكبر من الحد المسموح (5 MB).")
    # Do not accept DTD/entity declarations. SVG does not need them for normal stickers.
    upper = svg_bytes.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ConversionError("ملف SVG يحتوي على تعريفات XML غير مسموح بها.")
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError as exc:
        raise ConversionError("ملف SVG غير سليم.") from exc
    if root.tag.split("}")[-1].lower() != "svg":
        raise ConversionError("الملف لا يحتوي على عنصر SVG رئيسي صحيح.")

    forbidden = {"script", "style", "image", "foreignobject", "iframe", "object", "embed", "audio", "video"}
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag.split("}")[-1].lower() in forbidden:
                parent.remove(child)
        for key in list(parent.attrib):
            local = key.split("}")[-1].lower()
            value = parent.attrib[key].strip()
            lowered = value.lower()
            if local.startswith("on"):
                del parent.attrib[key]
                continue
            if local in {"href", "src"} and value and not value.startswith("#"):
                # Disallow external, data, and file references; keep local fragment links only.
                del parent.attrib[key]
                continue
            if "url(" in lowered:
                # Allow only local fragment URLs such as url(#gradient).
                urls = re.findall(r"url\(\s*(['\"]?)(.*?)\1\s*\)", lowered)
                if not urls or any(not target.startswith("#") for _, target in urls):
                    del parent.attrib[key]
    return ET.tostring(root, encoding="utf-8")


def _svg_to_png(source: Path, destination: Path) -> None:
    """Rasterize sanitized SVG using resvg (portable wheels), with Cairo fallback."""
    try:
        cleaned = _sanitize_svg(source.read_bytes())
        if resvg_py is not None:
            png_bytes = resvg_py.svg_to_bytes(
                svg_string=cleaned.decode("utf-8"),
                width=1024,
            )
            if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("SVG renderer did not return a PNG image")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(png_bytes)
            return
        if cairosvg is not None:
            cairosvg.svg2png(bytestring=cleaned, write_to=str(destination), output_width=1024)
            return
        raise ConversionError(
            "تحويل SVG يحتاج مكتبة resvg_py. شغّل تثبيت المتطلبات من requirements-vector.txt ثم أعد التشغيل."
        )
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError("لم أستطع تحويل رسم SVG. تأكد أن الملف سليم وأنه لا يعتمد على موارد خارجية.") from exc


def _pdf_to_images(source: Path, output_dir: Path, max_pages: int) -> list[Path]:
    if fitz is None:
        raise ConversionError("استخراج PDF يحتاج تثبيت المتطلبات الاختيارية من requirements-pdf.txt.")
    try:
        doc = fitz.open(source)
    except Exception as exc:
        raise ConversionError("ملف PDF تالف أو محمي بكلمة مرور.") from exc
    if doc.needs_pass:
        doc.close()
        raise ConversionError("ملف PDF محمي بكلمة مرور؛ أرسل نسخة غير محمية.")
    if len(doc) == 0:
        doc.close()
        raise ConversionError("ملف PDF فارغ.")
    paths = []
    for i, page in enumerate(doc[:max_pages]):
        pix = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
        out = output_dir / f"page_{i + 1:02d}.png"
        pix.save(out)
        paths.append(out)
    doc.close()
    return paths


def convert_lottie_json_to_tgs(
    source: Path, destination: Path, max_bytes: int = 64 * 1024
) -> dict[str, Any]:
    """Wrap a Telegram-compatible Bodymovin/Lottie JSON file as gzipped TGS."""
    if source.stat().st_size > 2 * 1024 * 1024:
        raise ConversionError("ملف Lottie JSON أكبر من الحد المسموح (2 MB).")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError("ملف Lottie JSON غير صالح.") from exc

    required = ("v", "fr", "ip", "op", "w", "h", "layers")
    if not isinstance(data, dict) or any(key not in data for key in required):
        raise ConversionError("ملف JSON لا يحتوي على بنية Lottie/Bodymovin المطلوبة.")
    try:
        width, height = int(data["w"]), int(data["h"])
        frame_rate = float(data["fr"])
        duration = (float(data["op"]) - float(data["ip"])) / frame_rate
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ConversionError("بيانات أبعاد أو توقيت Lottie غير صالحة.") from exc

    if width != 512 or height != 512:
        raise ConversionError("ملصق TGS يجب أن يكون 512×512؛ هذا الملف ليس على المقاس المطلوب.")
    if frame_rate != 60:
        raise ConversionError("ملصق TGS يتطلب 60 إطارًا في الثانية.")
    if duration <= 0 or duration > 3.05:
        raise ConversionError("مدة TGS يجب ألا تتجاوز 3 ثوانٍ.")
    if not isinstance(data.get("layers"), list):
        raise ConversionError("بنية طبقات Lottie غير صالحة.")

    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    if len(compressed) > max_bytes:
        raise ConversionError("ملف الحركة أكبر من حد TGS (64KB) بعد الضغط.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(compressed)
    return {
        "format": "TGS",
        "width": 512,
        "height": 512,
        "duration": duration,
        "size": len(compressed),
    }


def _validate_tgs(source: Path) -> dict[str, Any]:
    if source.stat().st_size > 64 * 1024:
        raise ConversionError("ملف TGS أكبر من الحد الرسمي وهو 64 KB.")
    try:
        with gzip.open(source, "rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("expanded too large")
        obj = json.loads(raw.decode("utf-8"))
        width, height = int(obj.get("w", 0)), int(obj.get("h", 0))
        fps = float(obj.get("fr", 0))
        duration = (float(obj.get("op", 0)) - float(obj.get("ip", 0))) / fps if fps else 99
        if width != 512 or height != 512 or fps != 60 or duration > 3.05 or duration <= 0 or not isinstance(obj.get("layers"), list):
            raise ValueError("TGS dimensions/duration invalid")
    except Exception as exc:
        raise ConversionError("ملف TGS غير صالح أو لا يطابق أبعاد/مدة تيليجرام.") from exc
    return {"format": "TGS", "width": 512, "height": 512, "duration": duration, "size": source.stat().st_size}


def _ahash(source: Path) -> int | None:
    try:
        with Image.open(source) as im:
            im.seek(0)
            small = ImageOps.grayscale(im.copy()).resize((8, 8), Image.Resampling.LANCZOS)
            values = list(small.tobytes())
        avg = sum(values) / len(values)
        result = 0
        for value in values:
            result = (result << 1) | (1 if value >= avg else 0)
        return result
    except Exception:
        return None


def _is_duplicate(path: Path, seen_hashes: set[str], seen_ahashes: list[int]) -> bool:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest in seen_hashes:
        return True
    seen_hashes.add(digest)
    ah = _ahash(path)
    if ah is not None:
        # Only suppress extremely close perceptual duplicates to avoid false positives.
        if any((ah ^ other).bit_count() <= 2 for other in seen_ahashes):
            return True
        seen_ahashes.append(ah)
    return False


def _extract_safe_archive(source: Path, target: Path, max_items: int, max_expanded_bytes: int) -> list[Path]:
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ConversionError("ملف ZIP غير سليم.") from exc
    members = [m for m in archive.infolist() if not m.is_dir()]
    if len(members) > max_items:
        archive.close()
        raise ConversionError(f"الأرشيف يحتوي على أكثر من {max_items} ملفًا.")
    total = sum(m.file_size for m in members)
    if total > max_expanded_bytes:
        archive.close()
        raise ConversionError("الحجم بعد فك الضغط أكبر من الحد الآمن.")
    results: list[Path] = []
    try:
        for idx, member in enumerate(members):
            # Reject traversal, absolute paths, symlinks, and suspicious compression ratios.
            p = PurePosixPath(member.filename.replace("\\", "/"))
            if p.is_absolute() or ".." in p.parts or member.external_attr >> 16 & 0o170000 == 0o120000:
                continue
            if member.file_size > 0 and member.compress_size == 0:
                continue
            if member.compress_size and member.file_size / member.compress_size > 1000:
                continue
            ext = Path(member.filename).suffix.lower()
            if ext not in DOC_MEDIA_EXTS:
                continue
            out = target / f"asset_{idx:03d}{ext}"
            try:
                with archive.open(member) as src, out.open("wb") as dst:
                    copied = 0
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > member.file_size or copied > max_expanded_bytes:
                            raise ConversionError("تجاوز ملف داخل الأرشيف الحجم المعلن.")
                        dst.write(chunk)
                results.append(out)
            except (OSError, zipfile.BadZipFile, RuntimeError):
                out.unlink(missing_ok=True)
    finally:
        archive.close()
    if not results:
        raise ConversionError("لم أجد صورًا أو فيديوهات أو ملفات PDF مدعومة داخل الأرشيف.")
    return results


def _convert_single(
    source: Path, name: str, output_dir: Path, *, ffmpeg_path: str, ffprobe_path: str,
    timeout: int, enable_bg_removal: bool, max_pdf_pages: int = 12,
    telegram_kind: str = "",
) -> list[OutputFile]:
    ext = Path(name).suffix.lower()
    safe = Path(_safe_name(name)).stem or "sticker"
    if ext == ".json":
        out = output_dir / f"{safe}.tgs"
        info = convert_lottie_json_to_tgs(source, out)
        return [
            OutputFile(
                out,
                "TGS",
                f"Lottie JSON إلى TGS — 512×512 — {info['duration']:.1f} ث — "
                f"{info['size'] // 1024} KB",
            )
        ]

    if ext == ".tgs":
        _validate_tgs(source)
        out = output_dir / f"{safe}.tgs"
        shutil.copy2(source, out)
        return [OutputFile(out, "TGS", "ملف TGS أصلي صالح؛ تم الحفاظ عليه دون تغيير.")]

    if ext == ".webp" and telegram_kind == "sticker":
        try:
            info = validate_webp_sticker(source)
        except ConversionError:
            # A non-conforming upload is repaired by the normal image pipeline.
            pass
        else:
            out = output_dir / f"{safe}.webp"
            shutil.copy2(source, out)
            return [OutputFile(out, "WEBP", f"ملصق ثابت أصلي صالح — {info['size'] // 1024} KB")]

    if ext == ".pdf":
        pages_dir = output_dir / f"{safe}_pages"
        pages_dir.mkdir(exist_ok=True)
        pages = _pdf_to_images(source, pages_dir, max_pdf_pages)
        outputs = []
        for i, page in enumerate(pages, 1):
            out = output_dir / f"{safe}_page_{i:02d}.webp"
            info = convert_image_to_webp(page, out, enable_bg_removal=False)
            outputs.append(OutputFile(out, "WEBP", f"صفحة PDF رقم {i} — 512×512 — {info['size'] // 1024} KB"))
        return outputs

    if ext in VECTOR_EXTS:
        png = output_dir / f"{safe}_raster.png"
        _svg_to_png(source, png)
        out = output_dir / f"{safe}.webp"
        info = convert_image_to_webp(png, out, enable_bg_removal=enable_bg_removal)
        png.unlink(missing_ok=True)
        return [OutputFile(out, "WEBP", f"SVG إلى WEBP — 512×512 — {info['size'] // 1024} KB")]

    if ext in VIDEO_EXTS or ext in ANIM_EXTS:
        out = output_dir / f"{safe}.webm"
        if telegram_kind == "sticker" and ext == ".webm":
            # A WEBM sticker already left Telegram's validator intact.
            info = validate_webm_sticker(source, ffprobe_path, min(30, max(5, timeout)))
            shutil.copy2(source, out)
            details = f"{info['size'] // 1024} KB"
            if info["probed"]:
                details = (
                    f"{info['width']}×{info['height']} — "
                    f"{info['duration']:.1f} ث — {details}"
                )
            return [OutputFile(out, "WEBM", f"ملصق فيديو أصلي صالح — {details}")]
        info = convert_video_to_webm(source, out, ffmpeg_path, ffprobe_path, timeout)
        return [OutputFile(out, "WEBM", f"VP9 — {info['width']}×{info['height']} — {info['duration']:.1f} ث — {info['size'] // 1024} KB")]

    if ext in AUDIO_EXTS:
        out = output_dir / f"{safe}_waveform.webm"
        info = convert_audio_to_webm(source, out, ffmpeg_path, ffprobe_path, timeout)
        return [OutputFile(out, "WEBM", f"موجة صوت متحركة — VP9 — 512×512 — {info['size'] // 1024} KB")]

    # Detect actual image content even when the file extension is missing or wrong.
    try:
        with Image.open(source) as im:
            animated = getattr(im, "n_frames", 1) > 1
            im_format = (im.format or "").upper()
    except Exception:
        animated, im_format = False, ""

    if animated:
        out = output_dir / f"{safe}.webm"
        info = convert_video_to_webm(source, out, ffmpeg_path, ffprobe_path, timeout)
        return [OutputFile(out, "WEBM", f"تحويل متحرك إلى VP9 — {info['duration']:.1f} ث — {info['size'] // 1024} KB")]
    if im_format in {"PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF", "AVIF"} or ext in IMAGE_EXTS:
        out = output_dir / f"{safe}.webp"
        info = convert_image_to_webp(source, out, enable_bg_removal)
        note = f"WEBP — 512×512 — {info['size'] // 1024} KB"
        if info["background_removed"]:
            note += " — تمت إزالة الخلفية بالذكاء الاصطناعي"
        elif not info["subject_outline"]:
            note += " — احتفظت بالخلفية الأصلية؛ لم أتمكن من فصل العنصر تلقائيًا"
        else:
            note += " — شفافية وحد أبيض وظل"
        return [OutputFile(out, "WEBP", note)]

    raise ConversionError("نوع الملف غير مدعوم. أرسل صورة أو GIF أو فيديو أو SVG أو PDF أو ZIP أو Word/PowerPoint يحتوي على صور أو TGS.")


def process_file(
    source: Path, original_name: str, output_dir: Path, settings: Any,
    *, telegram_kind: str = "",
) -> list[OutputFile]:
    """Process one upload, extracting supported media from archives/documents safely.

    ``telegram_kind`` lets a sticker that Telegram already validated keep its
    original bytes instead of being re-encoded.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(original_name).suffix.lower()
    seen_hashes: set[str] = set()
    seen_ahashes: list[int] = []

    if ext in ARCHIVE_EXTS:
        extracted_dir = output_dir / "extracted"
        extracted_dir.mkdir(exist_ok=True)
        if ext == ".zip":
            inputs = _extract_safe_archive(
                source, extracted_dir, settings.max_archive_items,
                settings.max_expanded_mb * 1024 * 1024
            )
        else:
            # Office/OpenDocument files are ZIP containers. Only extract embedded media.
            try:
                with zipfile.ZipFile(source) as zf:
                    all_items = zf.infolist()
                    if len(all_items) > settings.max_archive_items * 5 or sum(x.file_size for x in all_items) > settings.max_expanded_mb * 1024 * 1024:
                        raise ConversionError("المستند يحتوي على وسائط أكبر من الحد الآمن.")
                    media_items = [m for m in all_items if not m.is_dir() and Path(m.filename).suffix.lower() in DOC_MEDIA_EXTS]
                    if ext == ".docx":
                        media_items = [m for m in media_items if m.filename.startswith("word/media/")]
                    elif ext == ".pptx":
                        media_items = [m for m in media_items if m.filename.startswith("ppt/media/")]
                    elif ext == ".xlsx":
                        media_items = [m for m in media_items if m.filename.startswith("xl/media/")]
                    elif ext in {".odt", ".odp", ".ods"}:
                        media_items = [m for m in media_items if m.filename.startswith("Pictures/")]
                    inputs = []
                    if len(media_items) > settings.max_archive_items:
                        raise ConversionError("المستند يحتوي على عدد كبير جدًا من الصور.")
                    for i, member in enumerate(media_items):
                        out = extracted_dir / f"embedded_{i:03d}{Path(member.filename).suffix.lower()}"
                        with zf.open(member) as src, out.open("wb") as dst:
                            shutil.copyfileobj(src, dst, length=1024 * 1024)
                        inputs.append(out)
            except ConversionError:
                raise
            except Exception as exc:
                raise ConversionError("لم أستطع قراءة المستند أو استخراج الصور منه.") from exc
        results: list[OutputFile] = []
        errors: list[str] = []
        for i, item in enumerate(inputs[:settings.max_archive_items], 1):
            if _is_duplicate(item, seen_hashes, seen_ahashes):
                continue
            try:
                converted = _convert_single(
                    item, item.name, output_dir, ffmpeg_path=settings.ffmpeg_path,
                    ffprobe_path=settings.ffprobe_path, timeout=settings.max_processing_seconds,
                    enable_bg_removal=settings.enable_bg_removal, max_pdf_pages=settings.max_pdf_pages,
                )
                for out in converted:
                    # Prefix all outputs to avoid same-name collisions in the result zip.
                    prefixed = output_dir / f"{i:02d}_{out.path.name}"
                    if out.path != prefixed:
                        out.path.replace(prefixed)
                    results.append(OutputFile(prefixed, out.kind, out.note))
            except ConversionError as exc:
                errors.append(f"{item.name}: {str(exc).splitlines()[0]}")
        if not results:
            reason = "لم أستطع استخراج أي ملف قابل للتحويل من المستند/الأرشيف."
            if errors:
                reason += "\n" + "\n".join(errors[:4])
            raise ConversionError(reason)
        return results

    return _convert_single(
        source, original_name, output_dir, ffmpeg_path=settings.ffmpeg_path,
        ffprobe_path=settings.ffprobe_path, timeout=settings.max_processing_seconds,
        enable_bg_removal=settings.enable_bg_removal, max_pdf_pages=settings.max_pdf_pages,
        telegram_kind=telegram_kind,
    )
