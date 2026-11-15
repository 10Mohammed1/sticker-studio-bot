from __future__ import annotations

import gzip
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from bot.intake import (
    MediaDownloadError,
    UnsupportedMediaError,
    select_media,
    sniff_local_file,
)


def _message(**overrides):
    base = {
        "photo": None,
        "document": None,
        "video": None,
        "animation": None,
        "video_note": None,
        "audio": None,
        "voice": None,
        "sticker": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class IntakeSnifferTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_png_misnamed_as_text_is_detected_from_bytes(self) -> None:
        path = self.dir / "really_an_image.txt"
        Image.new("RGB", (8, 8), (255, 0, 0)).save(path, "PNG")
        suffix, mime, kind = sniff_local_file(path)
        self.assertEqual((suffix, mime, kind), (".png", "image/png", "image"))

    def test_jpeg_and_gif_signatures(self) -> None:
        jpg = self.dir / "a.bin"
        Image.new("RGB", (8, 8), (0, 255, 0)).save(jpg, "JPEG")
        self.assertEqual(sniff_local_file(jpg)[0], ".jpg")

        gif = self.dir / "b.bin"
        Image.new("RGB", (8, 8), (0, 0, 255)).save(gif, "GIF")
        self.assertEqual(sniff_local_file(gif)[2], "animation")

    def test_docx_is_distinguished_from_plain_zip(self) -> None:
        docx = self.dir / "report.bin"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", "<w:document/>")
        self.assertEqual(sniff_local_file(docx), (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "archive"))

        plain = self.dir / "plain.zip"
        with zipfile.ZipFile(plain, "w") as archive:
            archive.writestr("readme.txt", "hello")
        self.assertEqual(sniff_local_file(plain)[0], ".zip")

    def test_svg_is_detected(self) -> None:
        svg = self.dir / "vector.bin"
        svg.write_text('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
        self.assertEqual(sniff_local_file(svg), (".svg", "image/svg+xml", "vector"))

    def test_tgs_is_detected_inside_gzip(self) -> None:
        tgs = self.dir / "anim.bin"
        payload = json.dumps({"v": "5.7", "fr": 60, "layers": [], "w": 512, "h": 512}).encode()
        tgs.write_bytes(gzip.compress(payload))
        self.assertEqual(sniff_local_file(tgs)[0], ".tgs")
        self.assertEqual(sniff_local_file(tgs)[2], "tgs")

    def test_lottie_json_is_detected_by_content(self) -> None:
        source = self.dir / "animation.txt"
        source.write_text(
            json.dumps(
                {"v": "5.7", "fr": 60, "ip": 0, "op": 60, "w": 512, "h": 512, "layers": []}
            ),
            encoding="utf-8",
        )
        self.assertEqual(sniff_local_file(source), (".json", "application/json", "lottie"))

    def test_empty_file_is_rejected(self) -> None:
        empty = self.dir / "empty.png"
        empty.write_bytes(b"")
        with self.assertRaises(MediaDownloadError):
            sniff_local_file(empty)

    def test_unknown_bytes_are_rejected(self) -> None:
        unknown = self.dir / "file.bin"
        unknown.write_bytes(b"not a supported media file" * 10)
        with self.assertRaises(UnsupportedMediaError):
            sniff_local_file(unknown)


class MediaSelectionTests(unittest.TestCase):
    def test_largest_photo_is_selected(self) -> None:
        small = SimpleNamespace(file_id="small", width=90, height=90, file_size=100)
        large = SimpleNamespace(file_id="large", width=1280, height=720, file_size=900)
        message = _message(photo=[small, large])
        selected, name, mime, kind = select_media(message)
        self.assertEqual(selected.file_id, "large")
        self.assertEqual((name, mime, kind), ("telegram_photo.jpg", "image/jpeg", "photo"))

    def test_sticker_variants_get_the_right_names(self) -> None:
        cases = [
            (SimpleNamespace(is_animated=True, is_video=False, file_id="1"), "telegram_sticker.tgs"),
            (SimpleNamespace(is_animated=False, is_video=True, file_id="2"), "telegram_sticker.webm"),
            (SimpleNamespace(is_animated=False, is_video=False, file_id="3"), "telegram_sticker.webp"),
        ]
        for sticker, expected in cases:
            selected, name, _mime, kind = select_media(_message(sticker=sticker))
            self.assertEqual(selected.file_id, sticker.file_id)
            self.assertEqual(name, expected)
            self.assertEqual(kind, "sticker")

    def test_empty_message_returns_none(self) -> None:
        selected, name, mime, kind = select_media(_message())
        self.assertIsNone(selected)
        self.assertEqual((name, mime, kind), ("", None, ""))


if __name__ == "__main__":
    unittest.main()
