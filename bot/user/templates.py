"""Saved editing recipes ("templates") that replay a known list of operations.

Templates store only JSON, never image bytes, so they stay tiny and can be
re-applied to any future upload. Replay is deterministic because every editor
tool is a pure Pillow function.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..editor.image_editor import EditorError, apply_operations

MAX_TEMPLATES = 20
MAX_OPERATIONS = 12


class TemplateError(ValueError):
    """A user-facing explanation of an invalid template."""


def normalize_operations(operations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate and trim a template before it is stored."""
    cleaned: list[dict[str, Any]] = []
    for operation in operations:
        action = str(operation.get("action") or "").strip()
        if not action:
            continue
        entry: dict[str, Any] = {"action": action}
        text = operation.get("text")
        if text:
            entry["text"] = str(text)[:80]
        options = operation.get("options")
        if isinstance(options, Mapping) and options:
            entry["options"] = {
                str(key): value
                for key, value in options.items()
                if isinstance(value, (str, int, float, bool))
            }
        cleaned.append(entry)
    if not cleaned:
        raise TemplateError("لا توجد خطوات لتحفظها. عدّل الصورة أولاً ثم احفظ القالب.")
    return cleaned[:MAX_OPERATIONS]


def serialize(operations: Iterable[Mapping[str, Any]]) -> str:
    return json.dumps(normalize_operations(operations), ensure_ascii=False)


def deserialize(raw: str | None) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError) as exc:
        raise TemplateError("ملف القالب غير صالح.") from exc
    if not isinstance(data, list):
        raise TemplateError("ملف القالب غير صالح.")
    return [dict(item) for item in data if isinstance(item, Mapping) and item.get("action")]


def describe(operations: Iterable[Mapping[str, Any]], limit: int = 8) -> str:
    """Arabic one-line summary such as ``قص ← نص ← علامة مائية``."""
    names = [str(item.get("action")) for item in operations][:limit]
    return " ← ".join(names) if names else "—"


def apply_template(source: Path, destination: Path, operations: list[dict[str, Any]]) -> Path:
    try:
        return apply_operations(source, destination, operations)
    except EditorError as exc:
        raise TemplateError(str(exc)) from exc


async def save_template(db: Any, user_id: int, title: str, operations: list[dict[str, Any]]) -> int:
    clean_title = " ".join((title or "").split())[:64]
    if not clean_title:
        raise TemplateError("اسم القالب فارغ.")
    rows = await db.get_templates(user_id)
    if len(rows) >= MAX_TEMPLATES:
        raise TemplateError(
            f"وصلت إلى {MAX_TEMPLATES} قالبًا. احذف قالبًا قديمًا ثم احفظ الجديد."
        )
    return await db.add_template(user_id, clean_title, serialize(operations))


async def apply_saved(db: Any, user_id: int, template_id: int) -> tuple[str, list[dict[str, Any]]]:
    row = await db.get_template(template_id, user_id)
    if row is None:
        raise TemplateError("القالب غير موجود.")
    return str(row["title"]), deserialize(str(row["config_json"]))


__all__ = [
    "MAX_OPERATIONS",
    "MAX_TEMPLATES",
    "TemplateError",
    "apply_saved",
    "apply_template",
    "describe",
    "deserialize",
    "normalize_operations",
    "save_template",
    "serialize",
]
