from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat

from bot.ai import light
from bot.ai.manager import (
    background_enabled,
    capabilities_report,
    installed,
    light_available,
    set_background,
)


class FakeSettingsDB:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    async def get_setting(self, key: str) -> str | None:
        return self.values.get(key)

    async def set_setting(self, key: str, value: str) -> None:
        self.values[key] = value


def _subject(size=(300, 200), box=(60, 50, 240, 150), colour=(200, 40, 40, 255)) -> Image.Image:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle(box, fill=colour)
    return image


class ContentBoxTests(unittest.TestCase):
    def test_transparent_margins_are_trimmed(self) -> None:
        box = light.content_bbox(_subject())
        self.assertLessEqual(box[0], 61)
        self.assertGreaterEqual(box[0], 59)
        self.assertGreaterEqual(box[2], 240)
        self.assertLessEqual(box[2], 241)

    def test_opaque_uniform_border_is_trimmed(self) -> None:
        image = Image.new("RGB", (240, 240), (255, 255, 255))
        ImageDraw.Draw(image).ellipse((60, 60, 180, 180), fill=(20, 20, 20))
        box = light.content_bbox(image.convert("RGBA"))
        self.assertGreater(box[0], 30)
        self.assertLess(box[2], 210)

    def test_full_photo_falls_back_to_full_canvas(self) -> None:
        image = Image.new("RGBA", (64, 48), (10, 20, 30, 255))
        self.assertEqual(light.content_bbox(image), (0, 0, 64, 48))

    def test_auto_frame_centres_on_512(self) -> None:
        framed = light.auto_frame(_subject())
        self.assertEqual(framed.size, (512, 512))
        self.assertEqual(framed.mode, "RGBA")
        alpha_box = framed.getchannel("A").getbbox()
        self.assertIsNotNone(alpha_box)
        left, top, right, bottom = alpha_box
        self.assertGreater(left, 10)
        self.assertGreater(top, 10)
        self.assertLess(right, 502)
        self.assertLess(bottom, 502)


class EnhanceTests(unittest.TestCase):
    def test_enhance_preserves_transparency_and_canvas(self) -> None:
        source = _subject(colour=(120, 120, 120, 255))
        result = light.enhance(source)
        self.assertEqual(result.size, (512, 512))
        self.assertEqual(result.getpixel((4, 4))[3], 0)
        self.assertGreater(result.getpixel((256, 256))[0], 100)

    def test_upscale_is_bounded_and_returns_canvas(self) -> None:
        result = light.upscale(_subject(), 4.0)
        self.assertEqual(result.size, (512, 512))
        big = light.upscale(Image.new("RGBA", (1500, 1500), (0, 0, 0, 255)), 4.0)
        self.assertLessEqual(max(big.size), light.MAX_UPSCALE)

    def test_flat_image_brightness_uses_white_behind_transparency(self) -> None:
        data = light.analyze(_subject())  # 40% red on transparency
        self.assertGreater(data["brightness"], 60)
        self.assertTrue(data["transparent"])
        self.assertNotIn("#000000", light.dominant_colours(_subject()))

    def test_analyze_reports_suggestions(self) -> None:
        data = light.analyze(_subject(size=(200, 200), box=(70, 70, 130, 130)))
        self.assertEqual((data["width"], data["height"]), (200, 200))
        self.assertIn("قص_تلقائي", data["suggestions"])
        self.assertIn("تكبير_ذكي", data["suggestions"])

    def test_describe_is_arabic(self) -> None:
        report = light.describe(_subject())
        self.assertIn("تحليل الصورة", report)
        self.assertIn("الأبعاد", report)


class LightToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "source.png"
        _subject().save(self.source)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_each_tool_writes_a_512_png(self) -> None:
        for action in ("auto", "enhance", "upscale", "trim"):
            with self.subTest(action=action):
                destination = self.root / f"{action}.png"
                light.run_light_tool(self.source, destination, action)
                with Image.open(destination) as output:
                    self.assertEqual(output.size, (512, 512))
                    self.assertEqual(output.mode, "RGBA")

    def test_unknown_tool_is_rejected(self) -> None:
        with self.assertRaises(light.LightAIError):
            light.run_light_tool(self.source, self.root / "bad.png", "magic")

    def test_describe_file_reads_from_disk(self) -> None:
        self.assertIn("تحليل", light.describe_file(self.source))


class CapabilityTests(unittest.TestCase):
    def test_installed_reports_known_names(self) -> None:
        found = installed()
        self.assertIn("rembg", found)
        self.assertIn("cv2", found)
        self.assertTrue(light_available())

    def test_report_mentions_light_ai(self) -> None:
        report = capabilities_report()
        self.assertIn("Light AI", report)

    def test_background_switch_prefers_override(self) -> None:
        async def scenario():
            db = FakeSettingsDB({"ai_background": "1"})
            enabled = await background_enabled(db)
            await set_background(db, False)
            disabled = await background_enabled(db)
            return enabled, disabled, db.values

        enabled, disabled, values = asyncio.run(scenario())
        self.assertTrue(enabled)
        self.assertFalse(disabled)
        self.assertEqual(values["ai_background"], "0")


class MomentStatsTests(unittest.TestCase):
    def test_enhance_raises_brightness(self) -> None:
        dark = _subject(colour=(30, 30, 30, 255))
        before = ImageStat.Stat(dark.convert("L")).mean[0]
        after = ImageStat.Stat(light.enhance(dark).convert("L")).mean[0]
        self.assertGreater(after, before)


if __name__ == "__main__":
    unittest.main()
