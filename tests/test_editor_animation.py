from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat

from bot.animation.effects import (
    ALIASES,
    EFFECT_LABELS,
    AnimationError,
    build_filter,
    clamp_duration,
    clamp_speed,
    normalize_effect,
    render_effect,
)
from bot.config import settings
from bot.editor.image_editor import (
    DEFAULT_WATERMARK,
    EditorError,
    apply_operations,
    edit_image,
)

NEW_ACTIONS = (
    "flip_h", "flip_v", "crop_45", "crop_169", "brightness", "contrast",
    "saturation", "watermark", "background",
)


class EditorExtensionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.png"
        image = Image.new("RGBA", (320, 180), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 100, 180), fill=(220, 40, 40, 255))
        draw.ellipse((120, 40, 280, 160), fill=(40, 110, 220, 255))
        image.save(self.source)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _edit(self, action: str, **kwargs) -> Image.Image:
        destination = self.root / f"{action}-{len(list(self.root.iterdir()))}.png"
        edit_image(self.source, destination, action, kwargs.pop("text", None),
                   kwargs.pop("options", None))
        return Image.open(destination).convert("RGBA")

    def test_all_new_actions_produce_512_canvas(self) -> None:
        for action in NEW_ACTIONS:
            with self.subTest(action=action):
                output = self._edit(action, text="نص" if action == "text" else None,
                                    options={"color": "white"} if action == "background" else None)
                self.assertEqual(output.size, (512, 512))
                self.assertEqual(output.mode, "RGBA")
                output.close()

    def test_flip_h_mirrors_left_and_right(self) -> None:
        flipped = self._edit("flip_h")
        # The source is 320×180 centred on the 512 canvas (x 96..416). The red
        # block sits on the left, so after mirroring the left side is empty.
        left = flipped.getpixel((110, 256))
        right = flipped.getpixel((400, 256))
        self.assertEqual(left[3], 0)
        self.assertEqual(right[3], 255)
        self.assertGreater(right[0], right[2])
        flipped.close()

    def test_background_fills_transparency(self) -> None:
        filled = self._edit("background", options={"color": "white"})
        self.assertEqual(filled.getpixel((8, 8)), (255, 255, 255, 255))
        filled.close()

    def test_brightness_and_contrast_change_pixels(self) -> None:
        base = self._edit("crop")
        brighter = self._edit("brightness", options={"factor": 1.5})
        self.assertGreater(
            ImageStat.Stat(brighter.convert("L")).mean[0],
            ImageStat.Stat(base.convert("L")).mean[0],
        )
        base.close()
        brighter.close()

    def test_watermark_defaults_to_brand_text(self) -> None:
        marked = self._edit("watermark")
        self.assertTrue(DEFAULT_WATERMARK)
        marked.close()

    def test_invalid_color_is_rejected(self) -> None:
        with self.assertRaises(EditorError):
            edit_image(self.source, self.root / "bad.png", "background",
                       options={"color": "chartreuse"})

    def test_empty_text_is_rejected(self) -> None:
        with self.assertRaises(EditorError):
            edit_image(self.source, self.root / "empty.png", "text", "   ")

    def test_apply_operations_replays_a_template(self) -> None:
        destination = self.root / "template.png"
        apply_operations(
            self.source,
            destination,
            [
                {"action": "crop"},
                {"action": "brightness", "options": {"factor": 1.2}},
                {"action": "watermark", "text": "قالب"},
            ],
        )
        with Image.open(destination) as output:
            self.assertEqual(output.size, (512, 512))


class AnimationFilterTest(unittest.TestCase):
    def test_every_effect_has_a_label_and_filter(self) -> None:
        for name in set(ALIASES.values()):
            with self.subTest(effect=name):
                self.assertIn(name, EFFECT_LABELS)
                graph = build_filter(name, 2.0, 1.0, 24)
                self.assertIn("yuva420p", graph)
                self.assertIn("fps=24", graph)

    def test_alias_zoom_maps_to_zoom_in(self) -> None:
        self.assertEqual(normalize_effect(" Zoom "), "zoom_in")
        self.assertEqual(normalize_effect("slide_left"), "slide_left")

    def test_unknown_effect_is_rejected(self) -> None:
        with self.assertRaises(AnimationError):
            build_filter("explode", 2.0, 1.0, 24)

    def test_duration_and_speed_are_clamped(self) -> None:
        self.assertEqual(clamp_duration(2.5), 2.5)
        for value in (0.2, 4.0, "x"):
            with self.subTest(value=value):
                with self.assertRaises(AnimationError):
                    clamp_duration(value)
        self.assertEqual(clamp_speed(0.01), 0.25)
        self.assertEqual(clamp_speed(99), 4.0)

    def test_fade_filter_uses_requested_duration(self) -> None:
        graph = build_filter("fade_out", 1.5, 1.0, 20)
        self.assertIn("fade=t=out", graph)
        self.assertIn("st=", graph)

    def test_zoom_out_starts_zoom_in(self) -> None:
        graph = build_filter("zoom_out", 3.0, 1.0, 30)
        self.assertIn("zoompan", graph)
        self.assertIn("max(zoom-", graph)

    def test_slide_and_pulse_graphs_are_distinct(self) -> None:
        graphs = {build_filter(name, 2.0, 1.0, 24)
                  for name in ("slide_left", "slide_right", "pulse", "rotate")}
        self.assertEqual(len(graphs), 4)


def _ffmpeg_binary() -> str | None:
    for candidate in (settings.ffmpeg_path, shutil.which("ffmpeg")):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _ffprobe_binary() -> str | None:
    for candidate in (settings.ffprobe_path, shutil.which("ffprobe")):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


FFMPEG = _ffmpeg_binary()
FFPROBE = _ffprobe_binary()


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe binaries are unavailable")
class AnimationRenderTest(unittest.TestCase):
    def test_new_effects_produce_telegram_video_stickers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            ImageDraw.Draw(image).ellipse((40, 40, 216, 216), fill=(30, 120, 220, 255))
            image.save(source)
            for effect, duration in (("zoom_out", 2.0), ("pulse", 1.0), ("slide_left", 2.0)):
                with self.subTest(effect=effect):
                    output = root / f"{effect}.webm"
                    result = render_effect(
                        source, output, effect,
                        ffmpeg=FFMPEG, ffprobe=FFPROBE,
                        timeout=90, duration=duration, speed=1.0, fps=24,
                    )
                    self.assertEqual(result["codec"], "VP9")
                    self.assertEqual((result["width"], result["height"]), (512, 512))
                    self.assertLessEqual(result["duration"], duration + 0.1)
                    self.assertLessEqual(result["size"], 256 * 1024)
                    self.assertTrue(result["alpha_plane"])
                    self.assertEqual(result["effect"], effect)


if __name__ == "__main__":
    unittest.main()
