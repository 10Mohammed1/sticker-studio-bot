from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from bot.database import Database
from bot.user import templates
from bot.user.templates import TemplateError


def _source(root: Path) -> Path:
    path = root / "source.png"
    image = Image.new("RGBA", (320, 240), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 280, 200), fill=(40, 120, 220, 255))
    image.save(path)
    return path


class OperationValidationTests(unittest.TestCase):
    def test_normalise_drops_empty_and_caps_length(self) -> None:
        operations = [{"action": f"a{index}"} for index in range(20)]
        operations.append({"action": ""})
        cleaned = templates.normalize_operations(operations)
        self.assertEqual(len(cleaned), templates.MAX_OPERATIONS)
        self.assertEqual(cleaned[0], {"action": "a0"})

    def test_normalise_keeps_text_and_scalar_options_only(self) -> None:
        cleaned = templates.normalize_operations([
            {"action": "text", "text": "مرحبا", "options": {"size": 30, "bad": [1]}},
        ])
        self.assertEqual(cleaned[0]["text"], "مرحبا")
        self.assertEqual(cleaned[0]["options"], {"size": 30})

    def test_empty_template_is_rejected(self) -> None:
        with self.assertRaises(TemplateError):
            templates.normalize_operations([])

    def test_serialize_round_trip(self) -> None:
        raw = templates.serialize([{"action": "crop"}, {"action": "sepia"}])
        self.assertEqual(templates.deserialize(raw), [{"action": "crop"}, {"action": "sepia"}])

    def test_invalid_json_is_rejected(self) -> None:
        for value in ("{not json", "null", '"text"'):
            with self.subTest(value=value):
                with self.assertRaises(TemplateError):
                    templates.deserialize(value)

    def test_describe_joins_action_names(self) -> None:
        self.assertEqual(
            templates.describe([{"action": "crop"}, {"action": "text"}]),
            "crop ← text",
        )
        self.assertEqual(templates.describe([]), "—")


class ApplyTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_template_replays_to_512_png(self) -> None:
        destination = self.root / "out.png"
        templates.apply_template(
            _source(self.root), destination,
            [{"action": "crop"}, {"action": "grayscale"}, {"action": "watermark", "text": "قالب"}],
        )
        with Image.open(destination) as output:
            self.assertEqual(output.size, (512, 512))
            self.assertEqual(output.mode, "RGBA")

    def test_invalid_operation_surfaces_as_template_error(self) -> None:
        with self.assertRaises(TemplateError):
            templates.apply_template(
                _source(self.root), self.root / "bad.png", [{"action": "nonsense"}]
            )


class FakeTemplateDB:
    def __init__(self, rows: int = 0) -> None:
        self.rows = [{"id": index + 1} for index in range(rows)]
        self.added: list[tuple] = []

    async def get_templates(self, user_id):
        return list(self.rows)

    async def add_template(self, user_id, title, config_json):
        self.added.append((user_id, title, config_json))
        return len(self.rows) + 1


class SaveTemplateTests(unittest.TestCase):
    def test_save_rejects_empty_title(self) -> None:
        with self.assertRaises(TemplateError):
            asyncio.run(templates.save_template(FakeTemplateDB(), 1, "  ", [{"action": "crop"}]))

    def test_save_limits_number_of_templates(self) -> None:
        db = FakeTemplateDB(rows=templates.MAX_TEMPLATES)
        with self.assertRaises(TemplateError):
            asyncio.run(templates.save_template(db, 1, "قالب", [{"action": "crop"}]))

    def test_save_serialises_operations(self) -> None:
        db = FakeTemplateDB()
        identifier = asyncio.run(
            templates.save_template(db, 5, "قالبي", [{"action": "crop"}])
        )
        self.assertEqual(identifier, 1)
        self.assertEqual(db.added[0][1], "قالبي")
        self.assertEqual(templates.deserialize(db.added[0][2]), [{"action": "crop"}])


class TemplateDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "templates.sqlite3")
        asyncio.run(self.db.init(maintenance_default=False))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_get_list_delete(self) -> None:
        async def scenario():
            first = await self.db.add_template(7, "واحد", '[{"action": "crop"}]')
            second = await self.db.add_template(7, "اثنان", '[{"action": "sepia"}]')
            rows = await self.db.get_templates(7)
            single = await self.db.get_template(first, 7)
            foreign = await self.db.get_template(first, 99)
            title, operations = await templates.apply_saved(self.db, 7, second)
            removed = await self.db.delete_template(first, 7)
            after = await self.db.get_templates(7)
            return first, second, rows, single, foreign, title, operations, removed, after

        first, second, rows, single, foreign, title, operations, removed, after = asyncio.run(scenario())
        self.assertGreater(first, 0)
        self.assertEqual([row["title"] for row in rows], ["اثنان", "واحد"])
        self.assertEqual(single["title"], "واحد")
        self.assertIsNone(foreign)
        self.assertEqual(title, "اثنان")
        self.assertEqual(operations, [{"action": "sepia"}])
        self.assertTrue(removed)
        self.assertEqual(len(after), 1)


if __name__ == "__main__":
    unittest.main()
