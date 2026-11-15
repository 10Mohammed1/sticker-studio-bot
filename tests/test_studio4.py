from __future__ import annotations

import asyncio
import ast
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from bot.editor.image_editor import EditorError, edit_image
from bot.animation.effects import AnimationError, render_effect
from bot.tasks.manager import TaskManager
from bot.tasks.runtime import run_tracked


class EditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.png"
        image = Image.new("RGBA", (320, 180), (0, 0, 0, 0))
        ImageDraw.Draw(image).ellipse((80, 20, 240, 160), fill=(30, 120, 220, 255))
        image.save(self.source)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_image_edit_actions_write_512_canvas_png(self) -> None:
        for action in ("crop", "resize", "rotate", "outline", "shadow", "blur",
                       "grayscale", "sepia", "sharpen", "text"):
            with self.subTest(action=action):
                destination = self.root / f"{action}.png"
                edit_image(self.source, destination, action, "Sticker Studio" if action == "text" else None)
                self.assertTrue(destination.exists())
                with Image.open(destination) as output:
                    self.assertEqual(output.size, (512, 512))
                    self.assertEqual(output.mode, "RGBA")

    def test_unknown_action_is_rejected(self) -> None:
        with self.assertRaises(EditorError):
            edit_image(self.source, self.root / "bad.png", "unknown")

    def test_long_text_is_rejected(self) -> None:
        with self.assertRaises(EditorError):
            edit_image(self.source, self.root / "text.png", "text", "x" * 81)


class AnimationTests(unittest.TestCase):
    def test_unknown_effect_is_rejected_before_ffmpeg(self) -> None:
        with self.assertRaises(AnimationError):
            render_effect(Path("does-not-exist.png"), Path("out.webm"), "unsupported")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "FFmpeg/FFprobe not installed")
    def test_zoom_shake_fade_create_valid_transparent_vp9_webm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            ImageDraw.Draw(image).ellipse((40, 40, 216, 216), fill=(30, 120, 220, 255))
            image.save(source)
            for effect in ("zoom", "shake", "fade"):
                with self.subTest(effect=effect):
                    output = root / f"{effect}.webm"
                    result = render_effect(source, output, effect, timeout=60)
                    self.assertEqual(result["codec"], "VP9")
                    self.assertEqual((result["width"], result["height"]), (512, 512))
                    self.assertLessEqual(result["duration"], 3.05)
                    self.assertLessEqual(result["size"], 256 * 1024)
                    self.assertTrue(result["alpha_plane"])
                    self.assertFalse(result["audio"])


class TaskManagerTests(unittest.TestCase):
    def test_semaphore_bounds_concurrency(self) -> None:
        async def scenario() -> int:
            manager = TaskManager(max_concurrent=2)
            active = 0
            peak = 0

            async def worker(user_id: int) -> None:
                nonlocal active, peak
                async with manager.slot(user_id):
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0.02)
                    active -= 1

            await asyncio.gather(*(worker(index) for index in range(8)))
            return peak

        self.assertLessEqual(asyncio.run(scenario()), 2)

    def test_cancel_user_cancels_active_work(self) -> None:
        async def scenario() -> bool:
            manager = TaskManager(max_concurrent=1)
            started = asyncio.Event()

            async def worker() -> None:
                async with manager.slot(77):
                    started.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(worker())
            await started.wait()
            self.assertEqual(manager.cancel_user(77), 1)
            result = await asyncio.gather(task, return_exceptions=True)
            return isinstance(result[0], asyncio.CancelledError) and manager.active_count == 0

        self.assertTrue(asyncio.run(scenario()))


class TrackedTaskTests(unittest.TestCase):
    def test_run_tracked_records_started_and_completed(self) -> None:
        class FakeDB:
            def __init__(self):
                self.events = []
            async def create_task(self, task_id, user_id, task_type):
                self.events.append(("created", task_type))
            async def update_task(self, task_id, status, progress=0, detail=""):
                self.events.append((status, progress))

        async def scenario():
            db = FakeDB()
            result = await run_tracked(db, 101, "test_job", lambda x: x * 2, 21)
            return result, db.events

        result, events = asyncio.run(scenario())
        self.assertEqual(result, 42)
        self.assertEqual(events[0], ("created", "test_job"))
        self.assertIn(("running", 5), events)
        self.assertIn(("completed", 100), events)


class DatabaseSchemaTests(unittest.TestCase):
    def test_v4_schema_adds_new_tables_without_removing_legacy_tables(self) -> None:
        source = Path(__file__).parents[1] / "bot" / "database.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        schema = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "executescript" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                schema = node.args[0].value
                break
        self.assertIsNotNone(schema)
        connection = sqlite3.connect(":memory:")
        connection.executescript(schema)
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        expected = {
            "users", "settings", "jobs", "packs", "user_preferences", "favorites",
            "task_queue", "sticker_assets", "pack_items", "edit_sessions", "templates",
            "ai_tasks", "edit_history", "schema_migrations",
        }
        self.assertTrue(expected.issubset(tables))
        connection.close()


if __name__ == "__main__":
    unittest.main()
