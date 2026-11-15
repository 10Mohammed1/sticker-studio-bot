"""Facade for the permanent-pack workflow (4.1).

``handlers`` only talks to this module: it decides whether auto-add is on,
pushes every converted output through :mod:`bot.packs.auto_add`, and renders
the short result message plus the pack link. When the user switches auto-add
off, conversion keeps working and nothing is added or created.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..config import settings
from ..stickers import attach_to_pack, sticker_format_for_path
from . import auto_add, deduplication, user_packs
from .auto_add import AddOutcome

log = logging.getLogger("sticker-studio.automanager")

STATUS_LABELS = {
    auto_add.STATUS_ADDED: "➕ أُضيف إلى الحزمة",
    auto_add.STATUS_CREATED: "🆕 حزمة جديدة",
    auto_add.STATUS_DUPLICATE: "♻️ مكرر — لم يُضف",
    auto_add.STATUS_SKIPPED: "➖ تخطي (ليس ملصقًا)",
    auto_add.STATUS_FAILED: "⚠️ فشلت الإضافة",
    "disabled": "⏸ الإضافة التلقائية متوقفة",
}
STATUS_ICONS = {
    auto_add.STATUS_ADDED: "➕",
    auto_add.STATUS_CREATED: "🆕",
    auto_add.STATUS_DUPLICATE: "♻️",
    auto_add.STATUS_SKIPPED: "➖",
    auto_add.STATUS_FAILED: "⚠️",
    "disabled": "⏸",
}
GROUP_LABELS = {
    "static": ("📦", "الثابتة", (user_packs.SLOT_STATIC,)),
    "animated": (
        "🎞️",
        "المتحركة",
        (user_packs.SLOT_ANIMATED, user_packs.SLOT_ANIMATED_TGS),
    ),
}


async def enabled(db: Any, user_id: int) -> bool:
    """Auto-add is on when the host allows it and the user did not opt out."""
    if not settings.auto_pack:
        return False
    preferences = await db.get_user_preferences(user_id)
    return bool(preferences.get("auto_pack", True))


async def set_enabled(db: Any, user_id: int, value: bool) -> bool:
    await db.set_user_preference(user_id, "auto_pack", bool(value))
    return bool(value)


async def handle_outputs(
    bot: Any,
    db: Any,
    user_id: int,
    outputs: Sequence[Any],
    *,
    bot_username: str,
    first_name: str | None = None,
    task_id: str | None = None,
    source_kind: str = "file",
) -> list[AddOutcome]:
    """Add every converted sticker to its permanent pack (or log a skip)."""
    if not outputs:
        return []

    # A pack the user chose explicitly with /newpack or /packs -> use is a
    # manual action, so it keeps working even when the automatic packs are
    # turned off. /endpack returns to the automatic behaviour below.
    active = await db.get_active_pack(user_id)
    if active.exists and active.title and not active.auto:
        return await _handle_explicit_pack(
            bot, db, user_id, outputs,
            bot_username=bot_username,
            task_id=task_id, source_kind=source_kind,
        )

    if not await enabled(db, user_id):
        for item in outputs:
            path = getattr(item, "path", None)
            if path is None:
                continue
            try:
                await db.record_conversion(
                    user_id, source_kind, path.name,
                    path.suffix.lstrip(".").upper() or "?",
                    "disabled", task_id=task_id,
                    detail="auto-add turned off",
                )
            except Exception:  # noqa: BLE001
                log.warning("could not log disabled conversion", exc_info=True)
        log.info("auto-add disabled for user=%s; %d file(s) delivered only",
                 user_id, len(outputs))
        return []

    outcomes: list[AddOutcome] = []
    for item in outputs:
        path = getattr(item, "path", None)
        if path is None:
            continue
        try:
            outcomes.append(
                await auto_add.add_to_user_pack(
                    bot, db, user_id, path,
                    bot_username=bot_username,
                    first_name=first_name,
                    source_kind=source_kind,
                    source_name=path.name,
                    task_id=task_id,
                )
            )
        except Exception:  # noqa: BLE001 - the file was already delivered
            log.exception("auto-add crashed for user=%s file=%s", user_id, path.name)
            outcomes.append(
                AddOutcome(
                    auto_add.STATUS_FAILED,
                    "⚠️ التحويل نجح لكن تعذرت الإضافة إلى الحزمة.",
                )
            )
    return outcomes


async def _handle_explicit_pack(
    bot: Any,
    db: Any,
    user_id: int,
    outputs: Sequence[Any],
    *,
    bot_username: str,
    task_id: str | None,
    source_kind: str,
) -> list[AddOutcome]:
    """Deliver into the pack the user explicitly chose with /newpack or /packs.

    The permanent pairs stay untouched while an explicit pack is active. The
    same SHA-256 index still prevents adding one sticker twice.
    """
    outcomes: list[AddOutcome] = []
    for item in outputs:
        path = getattr(item, "path", None)
        if path is None:
            continue
        sticker_format = sticker_format_for_path(path)
        output_format = path.suffix.lstrip(".").upper() or "?"
        digest = await deduplication.sha256_async(path)
        existing = await deduplication.find(db, user_id, digest)
        if existing:
            pack_row = (
                await db.get_user_pack(int(existing["pack_id"]), user_id)
                if existing.get("pack_id") else None
            )
            pack = user_packs.from_row(pack_row) if pack_row else None
            await db.record_conversion(
                user_id, source_kind, path.name, output_format, "duplicate",
                pack_id=pack.id if pack else None, task_id=task_id,
                detail="already present in the user's pack", sha256=digest,
            )
            outcomes.append(
                AddOutcome(
                    auto_add.STATUS_DUPLICATE,
                    "ℹ️ هذا الملصق موجود في حزمتك بالفعل، لم يُضف مرة ثانية.",
                    slot=pack.slot if pack else None,
                    title=pack.title if pack else None,
                    pack_name=pack.name if pack else None,
                    pack_id=pack.id if pack else None,
                )
            )
            continue

        try:
            result = await attach_to_pack(
                bot, db, user_id, path, bot_username, auto_title=None
            )
        except Exception:  # noqa: BLE001 - conversion already delivered
            log.exception("explicit pack add crashed user=%s file=%s", user_id, path.name)
            outcomes.append(
                AddOutcome(
                    auto_add.STATUS_FAILED,
                    "⚠️ التحويل نجح لكن تعذرت الإضافة إلى الحزمة.",
                )
            )
            continue

        current = await db.get_active_pack(user_id)
        if result.status in {auto_add.STATUS_CREATED, auto_add.STATUS_ADDED}:
            await deduplication.remember(
                db, user_id, digest, None, "", f"sha256:{digest}"
            )
        await db.record_conversion(
            user_id, source_kind, path.name, output_format, result.status,
            task_id=task_id, detail=result.message[:500], sha256=digest,
        )
        outcomes.append(
            AddOutcome(
                result.status,
                result.message,
                slot=user_packs.FORMAT_TO_SLOT.get(sticker_format or ""),
                title=current.title,
                pack_name=current.name,
            )
        )
    return outcomes


def summary_text(
    outcomes: Sequence[AddOutcome], *, converted: int, auto_enabled: bool
) -> str:
    """Short Arabic summary: what was converted and where it landed."""
    if converted <= 0:
        return ""
    head = (
        "✅ تم تحويل الصورة بنجاح."
        if converted == 1
        else f"✅ تم تحويل {converted} ملفات بنجاح."
    )
    if not auto_enabled:
        return (
            f"{head}\n"
            "ℹ️ لم تتم إضافة الملصق إلى الحزمة لأن الإضافة التلقائية متوقفة.\n"
            "يمكنك تفعيلها من ⚙️ إعدادات الحزم."
        )
    lines: list[str] = [head]
    seen: set[tuple[str, str | None]] = set()
    for outcome in outcomes:
        if not outcome.message:
            continue
        key = (outcome.status, outcome.pack_name)
        if key in seen:
            continue
        seen.add(key)
        lines.append(outcome.message)
    if len(lines) == 1:
        lines.append("ℹ️ لم تُضف أي ملصقات جديدة.")
    return "\n".join(lines)


def summary_keyboard(outcomes: Sequence[AddOutcome]) -> InlineKeyboardMarkup | None:
    """One button per distinct pack that received a sticker."""
    rows: list[list[InlineKeyboardButton]] = []
    seen: set[str] = set()
    for outcome in outcomes:
        if not outcome.added or not outcome.pack_name or outcome.pack_name in seen:
            continue
        seen.add(outcome.pack_name)
        label = (outcome.title or "حزمة الملصقات")[:52]
        rows.append([
            InlineKeyboardButton(text=f"🔗 {label}", url=outcome.link or "")
        ])
        if len(rows) >= 4:
            break
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# --------------------------------------------------------------------------
# UI rendering
# --------------------------------------------------------------------------
async def settings_view(db: Any, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    auto_enabled = await enabled(db, user_id)
    packs = await user_packs.list_packs(db, user_id)
    lines = [
        "⚙️ <b>إعدادات الحزم</b>",
        (
            "🟢 الإضافة التلقائية: <b>مفعّلة</b>"
            if auto_enabled
            else "🔴 الإضافة التلقائية: <b>متوقفة</b>"
        ),
        "",
        "عند التفعيل: كل صورة تُضاف إلى حزمتك الثابتة، وكل GIF/فيديو إلى حزمتك المتحركة.",
        "عند الإيقاف: يستمر التحويل وإرسال الملفات، ولا تُنشأ حزم تلقائيًا.",
        "وتبقى الحزمة التي تُنشئها بنفسك عبر /newpack تعمل دائمًا وتُضاف إليها ملصقاتك.",
    ]
    if not settings.auto_pack:
        lines.append("\n🔒 الحزم التلقائية معطّلة من إعدادات الاستضافة؛ الإنشاء عبر /newpack فقط.")
    if packs:
        lines.append("\n<b>حزمك الدائمة:</b>")
        for pack in packs:
            marker = "🔗" if pack.name else "…"
            lines.append(
                f"{user_packs.slot_emoji(pack.slot)} {pack.caption} — "
                f"{pack.sticker_count}/{user_packs.MAX_STICKERS_PER_PACK} {marker}"
            )
    return "\n".join(lines), settings_keyboard(auto_enabled)


def settings_keyboard(auto_enabled: bool) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton(
            text="🔴 إيقاف الإضافة التلقائية", callback_data="autopack:off"
        )
        if auto_enabled
        else InlineKeyboardButton(
            text="🟢 تفعيل الإضافة التلقائية", callback_data="autopack:on"
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle],
            [
                InlineKeyboardButton(text="📦 حزمتي الثابتة", callback_data="autopack:view:static"),
                InlineKeyboardButton(text="🎞️ حزمتي المتحركة", callback_data="autopack:view:animated"),
            ],
            [
                InlineKeyboardButton(text="📚 سجل التحويلات", callback_data="autopack:history"),
                InlineKeyboardButton(text="🔄 تحديث", callback_data="autopack:refresh"),
            ],
        ]
    )


async def packs_view(db: Any, user_id: int, group: str = "static") -> tuple[str, InlineKeyboardMarkup]:
    emoji, label, slots = GROUP_LABELS.get(group, GROUP_LABELS["static"])
    packs = [pack for pack in await user_packs.list_packs(db, user_id) if pack.slot in slots]
    rows: list[list[InlineKeyboardButton]] = []
    if not packs:
        text = (
            f"{emoji} <b>حزمتك {label}</b>\n"
            "لم تُنشأ بعد. أرسل أول ملف وستُنشأ تلقائيًا وتُعاد استخدامها دائمًا."
        )
    else:
        lines = [f"{emoji} <b>حزمتك {label}</b>", ""]
        for pack in packs:
            lines.append(
                f"• <b>{pack.title}</b>\n"
                f"  {pack.sticker_count}/{user_packs.MAX_STICKERS_PER_PACK} ملصق"
            )
            if pack.link:
                rows.append([
                    InlineKeyboardButton(
                        text=f"🔗 فتح {pack.caption}"[:60], url=pack.link
                    )
                ])
            else:
                lines.append("  ⏳ لم يُضف أي ملصق بعد")
        text = "\n".join(lines)
    rows.append([
        InlineKeyboardButton(text="⚙️ إعدادات الحزم", callback_data="autopack:settings"),
        InlineKeyboardButton(text="🔄 تحديث", callback_data="autopack:view:" + group),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def history_text(db: Any, user_id: int, limit: int = 8) -> str:
    rows = await db.get_conversions(user_id, limit)
    if not rows:
        return "📚 <b>سجل التحويلات</b>\nلا توجد عمليات بعد."
    lines = ["📚 <b>سجل التحويلات</b>", ""]
    for row in rows:
        status = str(row["status"])
        icon = STATUS_ICONS.get(status, "•")
        label = STATUS_LABELS.get(status, status)
        name = str(row["source_name"] or "")[:40]
        fmt = str(row["output_format"] or "").upper()
        lines.append(f"{icon} <code>{name}</code> — {fmt} — {label}")
    return "\n".join(lines)


__all__ = [
    "GROUP_LABELS",
    "STATUS_LABELS",
    "enabled",
    "handle_outputs",
    "history_text",
    "packs_view",
    "set_enabled",
    "settings_keyboard",
    "settings_view",
    "summary_keyboard",
    "summary_text",
]
