from __future__ import annotations

import gzip
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv
from PIL import Image

from bot.converter import (
    ConversionError,
    _sanitize_svg,
    convert_image_to_webp,
    process_file,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
FFMPEG = (os.getenv("FFMPEG_PATH") or "ffmpeg").strip() or "ffmpeg"
FFPROBE = (os.getenv("FFPROBE_PATH") or "ffprobe").strip() or "ffprobe"
HAS_FFMPEG = shutil.which(FFMPEG) is not None


def make_settings(tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
        max_processing_seconds=60,
        enable_bg_removal=False,
        max_archive_items=10,
        max_expanded_mb=20,
        max_pdf_pages=2,
    )


class ImageConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_png_becomes_512px_webp_within_telegram_limit(self) -> None:
        source = self.dir / "input.png"
        output = self.dir / "sticker.webp"
        Image.new("RGBA", (300, 200), (255, 0, 0, 128)).save(source, "PNG")

        info = convert_image_to_webp(source, output, enable_bg_removal=False)

        self.assertTrue(output.exists())
        self.assertEqual((info["width"], info["height"]), (512, 512))
        self.assertLessEqual(output.stat().st_size, 512 * 1024)
        with Image.open(output) as result:
            self.assertEqual(result.format, "WEBP")
            self.assertEqual(result.size, (512, 512))

    def test_process_file_returns_a_webp_output(self) -> None:
        source = self.dir / "photo.png"
        Image.new("RGB", (640, 480), (10, 20, 30)).save(source, "PNG")
        output_dir = self.dir / "out"

        results = process_file(source, source.name, output_dir, make_settings(self.dir))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].path.suffix, ".webp")
        self.assertTrue(results[0].path.exists())

    def test_zip_archive_is_extracted_and_converted(self) -> None:
        image = self.dir / "inside.png"
        Image.new("RGB", (64, 64), (1, 2, 3)).save(image, "PNG")
        archive = self.dir / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(image, arcname="folder/inside.png")

        results = process_file(
            archive, archive.name, self.dir / "out_zip", make_settings(self.dir)
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "WEBP")

    def test_tgs_is_validated_and_preserved(self) -> None:
        payload = json.dumps(
            {"v": "5.7", "fr": 60, "ip": 0, "op": 60, "w": 512, "h": 512, "layers": []}
        ).encode()
        source = self.dir / "anim.tgs"
        source.write_bytes(gzip.compress(payload))

        results = process_file(
            source, source.name, self.dir / "out_tgs", make_settings(self.dir)
        )

        self.assertEqual(results[0].kind, "TGS")
        self.assertEqual(results[0].path.read_bytes(), source.read_bytes())

    def test_lottie_json_becomes_tgs(self) -> None:
        source = self.dir / "animation.json"
        source.write_text(
            json.dumps(
                {"v": "5.7", "fr": 60, "ip": 0, "op": 60, "w": 512, "h": 512, "layers": []}
            ),
            encoding="utf-8",
        )

        results = process_file(
            source, source.name, self.dir / "out_json", make_settings(self.dir)
        )

        self.assertEqual(results[0].kind, "TGS")
        self.assertEqual(results[0].path.suffix, ".tgs")
        with gzip.open(results[0].path, "rb") as stream:
            payload = json.loads(stream.read().decode("utf-8"))
        self.assertEqual(payload["w"], 512)

    def test_lottie_json_with_wrong_frame_rate_is_rejected(self) -> None:
        source = self.dir / "slow.json"
        source.write_text(
            json.dumps(
                {"v": "5.7", "fr": 30, "ip": 0, "op": 30, "w": 512, "h": 512, "layers": []}
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConversionError):
            process_file(source, source.name, self.dir / "out_slow", make_settings(self.dir))

    def test_invalid_tgs_is_rejected(self) -> None:
        payload = json.dumps(
            {"v": "5.7", "fr": 30, "ip": 0, "op": 60, "w": 256, "h": 256, "layers": []}
        ).encode()
        source = self.dir / "bad.tgs"
        source.write_bytes(gzip.compress(payload))

        with self.assertRaises(ConversionError):
            process_file(source, source.name, self.dir / "out_bad", make_settings(self.dir))

    def test_static_sticker_is_preserved_when_already_valid(self) -> None:
        source = self.dir / "sticker.webp"
        Image.new("RGBA", (512, 512), (0, 0, 255, 128)).save(source, "WEBP", exact=True)

        results = process_file(
            source, source.name, self.dir / "out_webp_sticker", make_settings(self.dir),
            telegram_kind="sticker",
        )

        self.assertEqual(results[0].kind, "WEBP")
        self.assertEqual(results[0].path.read_bytes(), source.read_bytes())

    def test_non_512_sticker_is_reprocessed(self) -> None:
        source = self.dir / "small.webp"
        Image.new("RGBA", (256, 256), (0, 255, 0, 255)).save(source, "WEBP")

        results = process_file(
            source, source.name, self.dir / "out_small", make_settings(self.dir),
            telegram_kind="sticker",
        )

        with Image.open(results[0].path) as output:
            self.assertEqual(output.size, (512, 512))

    def test_video_sticker_is_preserved_without_reencoding(self) -> None:
        source = self.dir / "sticker.webm"
        source.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 256 + b"V_VP9")

        results = process_file(
            source, source.name, self.dir / "out_sticker", make_settings(self.dir),
            telegram_kind="sticker",
        )

        self.assertEqual(results[0].kind, "WEBM")
        self.assertEqual(results[0].path.read_bytes(), source.read_bytes())

    def test_video_sticker_with_audio_track_is_rejected(self) -> None:
        source = self.dir / "noisy.webm"
        source.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64 + b"V_VP9" + b"A_OPUS")

        with self.assertRaises(ConversionError):
            process_file(
                source, source.name, self.dir / "out_noisy", make_settings(self.dir),
                telegram_kind="sticker",
            )

    def test_oversized_video_sticker_is_rejected(self) -> None:
        source = self.dir / "big.webm"
        source.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * (257 * 1024) + b"V_VP9")

        with self.assertRaises(ConversionError):
            process_file(
                source, source.name, self.dir / "out_big", make_settings(self.dir),
                telegram_kind="sticker",
            )

    def test_svg_sanitizer_removes_scripts_and_external_links(self) -> None:
        svg = b"""<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">
            <script>alert(1)</script>
            <image href="https://example.com/x.png"/>
            <rect width="10" height="10" fill="url(#grad)"/>
        </svg>"""
        cleaned = _sanitize_svg(svg).decode()
        self.assertNotIn("script", cleaned)
        self.assertNotIn("onload", cleaned)
        self.assertNotIn("example.com", cleaned)
        self.assertIn("url(#grad)", cleaned)

    @unittest.skipUnless(HAS_FFMPEG, "FFmpeg is not installed on this machine")
    def test_gif_becomes_video_webm(self) -> None:
        frames = [
            Image.new("RGB", (64, 64), (255, 0, 0)),
            Image.new("RGB", (64, 64), (0, 0, 255)),
        ]
        source = self.dir / "anim.gif"
        frames[0].save(source, save_all=True, append_images=frames[1:], duration=100, loop=0)

        results = process_file(
            source, source.name, self.dir / "out_gif", make_settings(self.dir)
        )

        self.assertEqual(results[0].kind, "WEBM")
        self.assertLessEqual(results[0].path.stat().st_size, 256 * 1024)


if __name__ == "__main__":
    unittest.main()
