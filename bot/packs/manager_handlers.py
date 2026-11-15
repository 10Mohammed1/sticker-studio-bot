"""Pack Manager Pro routes: live Telegram pack editing inside the chat.

Everything here reads the pack from Telegram first, mutates it, then re-reads and
mirrors the order locally. Telegram errors are translated into Arabic guidance by
:mod:`bot.packs.manager`, so a failing button never leaves the user without a
next step.
"""
from __future__ import annotations

import html
import logging
from typing import Sequence

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..database import Database
from .exporter import PackExportError, export_pack
from .manager import (
    MAX_STICKERS,
    PackInfo,
    PackManagerError,
    copy_pack,
    delete_sticker,
    fetch_pack,
    find_duplicates,
    merge_into,
    move_sticker,
    normalize_title,
    rename_pack,
)
from .organizer import duplicates_in, sync_pack

log = logging.getLogger("sticker-studio.packs")
router = Router(name="sticker-studio-pack-manager")

PAGE_SIZE = 6


class PackManagerStates(StatesGroup):
    waiting_copy_title = State()
    waiting_rename_title = State()


def _short(text: str, limit: int = 40) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _pack_menu(pack: PackInfo, pack_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"📋 الملصقات ({pack.count})",
                callback_data=f"pkm:list:{pack_id}:0",
            )
        ]
    ]
    if pack.bot_owned:
        rows.append(
            [
                InlineKeyboardButton(text="📄 نسخ الحزمة", callback_data=f"pkm:copy:{pack_id}"),
                InlineKeyboardButton(text="🔗 دمج حزمة", callback_data=f"pkm:mergepick:{pack_id}"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="✏️ تغيير الاسم", callback_data=f"pkm:rename:{pack_id}"),
                InlineKeyboardButton(text="🔍 التكرارات", callback_data=f"pkm:dups:{pack_id}"),
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="📤 تصدير ZIP", callback_data=f"pkm:export:{pack_id}")]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔗 فتح في تيليجرام", url=f"https://t.me/addstickers/{pack.name}"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ رجوع للحزم", callback_data="pkm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _stickers_page(pack: PackInfo, pack_id: int, page: int) -> InlineKeyboardMarkup:
    pages = max(1, (pack.count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(start, min(start + PAGE_SIZE, pack.count)):
        sticker = pack.stickers[index]
        emoji = sticker.emoji or "•"
        label = f"{index + 1}. {emoji} {sticker.file_unique_id[:6]}"
        controls = [InlineKeyboardButton(text=label[:40], callback_data="pkm:noop")]
        if pack.bot_owned:
            controls.extend(
                [
                    InlineKeyboardButton(text="🗑", callback_data=f"pkm:del:{pack_id}:{index}"),
                    InlineKeyboardButton(text="⬆️", callback_data=f"pkm:up:{pack_id}:{index}"),
                    InlineKeyboardButton(text="⬇️", callback_data=f"pkm:down:{pack_id}:{index}"),
                ]
            )
        rows.append(controls)
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="◀️ السابق", callback_data=f"pkm:list:{pack_id}:{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="pkm:noop")
    )
    if page < pages - 1:
        navigation.append(
            InlineKeyboardButton(text="التالي ▶️", callback_data=f"pkm:list:{pack_id}:{page + 1}")
        )
    rows.append(navigation)
    rows.append(
        [InlineKeyboardButton(text="⬅️ رجوع للحزمة", callback_data=f"pkm:open:{pack_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _packs_keyboard(packs: Sequence[object]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_short(f"📦 {getattr(pack, 'title', 'حزمة')}", 55),
                callback_data=f"pkm:open:{getattr(pack, 'id')}",
            )
        ]
        for pack in packs
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[
        InlineKeyboardButton(text="لا توجد حزم", callback_data="pkm:noop")
    ]])


def _panel_text(pack: PackInfo) -> str:
    lines = [
        f"🛠 <b>{html.escape(pack.title)}</b>",
        f"النوع: {pack.format_label}",
        f"عدد الملصقات: {pack.count}/{MAX_STICKERS}",
        f"الاسم: <code>{html.escape(pack.name)}</code>",
    ]
    if pack.bot_owned:
        lines.append("✅ البوت يملك هذه الحزمة ويمكنه تعديلها.")
    else:
        lines.append(
            "ℹ️ هذه الحزمة لم ينشئها البوت، لذلك لا يمكن تعديلها من هنا — "
            "التصدير متاح فقط."
        )
    return "\n".join(lines)


async def _load_pack(bot: Bot, db: Database, user_id: int, pack_id: int,
                     bot_username: str) -> PackInfo:
    row = await db.get_pack(pack_id, user_id)
    if row is None:
        raise PackManagerError("لم أجد هذه الحزمة في سجلك.")
    if not row.name:
        raise PackManagerError("هذه الحزمة لم تُنشأ في تيليجرام بعد. أضف ملصقًا أولًا.")
    pack = await fetch_pack(bot, row.name, bot_username)
    await sync_pack(db, pack_id, pack)
    return pack


@router.message(Command("g", "packmgr"))
async def pack_manager_entry(message: Message, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    packs = await db.get_packs(user.id)
    if not packs:
        await message.answer(
            "لا توجد حزم بعد. أرسل صورة أو استخدم /newpack ثم عد إلى /packmgr."
        )
        return
    await message.answer(
        "🛠 <b>إدارة الحزم المتقدمة</b>\n"
        "اختر حزمة لعرض ملصقاتها وحذفها أو ترتيبها أو نسخها أو دمجها:",
        reply_markup=_packs_keyboard(packs),
    )


@router.callback_query(F.data.startswith("pkm:"))
async def pack_manager_callbacks(
    callback: CallbackQuery, bot: Bot, db: Database, state: FSMContext,
    bot_username: str,
) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    user_id = callback.from_user.id

    if action == "noop":
        await callback.answer()
        return
    if action == "home":
        packs = await db.get_packs(user_id)
        await state.clear()
        await _swap(callback, "🛠 <b>إدارة الحزم المتقدمة</b>\nاختر حزمة:", _packs_keyboard(packs))
        await callback.answer()
        return

    try:
        pack_id = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("طلب غير صالح.", show_alert=True)
        return

    try:
        if action == "copy":
            await state.set_state(PackManagerStates.waiting_copy_title)
            await state.update_data(pack_id=pack_id)
            await callback.answer()
            if callback.message:
                await callback.message.answer(
                    "أرسل اسم الحزمة الجديدة (1-64 حرفًا). /cancel للإلغاء."
                )
            return
        if action == "rename":
            await state.set_state(PackManagerStates.waiting_rename_title)
            await state.update_data(pack_id=pack_id)
            await callback.answer()
            if callback.message:
                await callback.message.answer(
                    "أرسل الاسم الجديد للحزمة (1-64 حرفًا). /cancel للإلغاء."
                )
            return

        pack = await _load_pack(bot, db, user_id, pack_id, bot_username)

        if action == "open":
            await _swap(callback, _panel_text(pack), _pack_menu(pack, pack_id))
        elif action == "list":
            page = int(parts[3]) if len(parts) > 3 else 0
            try:
                await _swap(
                    callback,
                    _panel_text(pack) + "\n\nاختر ملصقًا لحذفه أو تحريكه:",
                    _stickers_page(pack, pack_id, page),
                )
            except ValueError:
                await callback.answer("طلب غير صالح.", show_alert=True)
                return
        elif action == "del":
            index = int(parts[3])
            removed = await delete_sticker(bot, pack, index)
            await _reload_after_change(
                callback, bot, db, user_id, pack_id, bot_username,
                f"🗑 حُذف الملصق رقم {index + 1} ({removed.emoji or '•'}).",
            )
        elif action in {"up", "down"}:
            index = int(parts[3])
            target = index - 1 if action == "up" else index + 1
            if not 0 <= target < pack.count:
                await callback.answer("لا يمكن التحريك أكثر في هذا الاتجاه.", show_alert=True)
                return
            await move_sticker(bot, pack, index, target)
            await _reload_after_change(
                callback, bot, db, user_id, pack_id, bot_username,
                f"↕️ انتقل الملصق من {index + 1} إلى {target + 1}.",
            )
        elif action == "mergepick":
            await _show_merge_sources(callback, db, user_id, pack, pack_id)
        elif action == "merge":
            source_id = int(parts[3])
            source = await _load_pack(bot, db, user_id, source_id, bot_username)
            added = await merge_into(bot, user_id, pack, source)
            await _reload_after_change(
                callback, bot, db, user_id, pack_id, bot_username,
                f"🔗 تم دمج {added} ملصقًا من «{_short(source.title)}».",
            )
        elif action == "dups":
            await _show_duplicates(callback, db, user_id, pack, pack_id, bot_username)
        elif action == "export":
            await _export(callback, bot, db, user_id, pack, pack_id)
        else:
            await callback.answer("إجراء غير معروف.", show_alert=True)
            return
    except PackManagerError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    except PackExportError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    except Exception:
        log.exception("Pack manager action %s failed for %s", action, user_id)
        await callback.answer("تعذر تنفيذ العملية على الحزمة.", show_alert=True)
        return
    await callback.answer()


async def _show_merge_sources(
    callback: CallbackQuery, db: Database, user_id: int, pack: PackInfo, pack_id: int
) -> None:
    rows = await db.get_packs(user_id)
    candidates = [row for row in rows if row.id != pack_id and row.name]
    buttons = [
        [
            InlineKeyboardButton(
                text=_short(f"📦 {row.title} — {row.sticker_format}", 55),
                callback_data=f"pkm:merge:{pack_id}:{row.id}",
            )
        ]
        for row in candidates
    ]
    if not buttons:
        await callback.answer("لا توجد حزمة أخرى للدمج.", show_alert=True)
        return
    buttons.append(
        [InlineKeyboardButton(text="⬅️ رجوع للحزمة", callback_data=f"pkm:open:{pack_id}")]
    )
    await _swap(
        callback,
        f"🔗 <b>دمج داخل «{html.escape(pack.title)}»</b>\n"
        "يجب أن تكون الحزمة المصدر بنفس الصيغة، وأن يتسع الحد الأقصى (50):",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def _show_duplicates(
    callback: CallbackQuery, db: Database, user_id: int, pack: PackInfo, pack_id: int,
    bot_username: str,
) -> None:
    inside = duplicates_in(pack)
    rows = await db.get_packs(user_id)
    others: list[PackInfo] = [pack]
    for row in rows[:5]:
        if row.id == pack_id or not row.name:
            continue
        try:
            others.append(await fetch_pack(callback.bot, row.name, bot_username))
        except PackManagerError:
            continue
    cross = find_duplicates(others)
    lines = [f"🔍 <b>تكرارات «{html.escape(pack.title)}»</b>"]
    if inside:
        lines.append(f"داخل الحزمة: {len(inside)} ملصقًا مكررًا (نفس الملف أكثر من مرة).")
    else:
        lines.append("داخل الحزمة: لا تكرار.")
    if cross:
        lines.append(f"مع حزمك الأخرى: {len(cross)} ملفًا مشتركًا.")
        for unique_id, names in list(cross.items())[:5]:
            lines.append(f"• <code>{html.escape(unique_id[:14])}</code> في {len(names)} حزمة")
    else:
        lines.append("مع حزمك الأخرى: لا تكرار.")
    await _swap(
        callback,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📋 عرض الملصقات", callback_data=f"pkm:list:{pack_id}:0")],
            [InlineKeyboardButton(text="⬅️ رجوع للحزمة", callback_data=f"pkm:open:{pack_id}")],
        ]),
    )


async def _export(
    callback: CallbackQuery, bot: Bot, db: Database, user_id: int,
    pack: PackInfo, pack_id: int,
) -> None:
    from ..config import settings
    import tempfile
    from pathlib import Path
    from aiogram.types import FSInputFile

    work = Path(tempfile.mkdtemp(prefix="sticker-pack-export-", dir=settings.work_dir))
    archive = work / f"{pack.name}.zip"
    try:
        manifest = await export_pack(bot, pack.name, archive)
        if callback.message:
            await callback.message.answer_document(
                FSInputFile(archive, filename=f"{pack.name}.zip"),
                caption=f"✅ صُدّر {manifest['sticker_count']} ملصقًا من «{html.escape(pack.title)}».",
            )
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


async def _reload_after_change(
    callback: CallbackQuery, bot: Bot, db: Database, user_id: int,
    pack_id: int, bot_username: str, notice: str,
) -> None:
    pack = await _load_pack(bot, db, user_id, pack_id, bot_username)
    await _swap(
        callback,
        f"{notice}\n\n{_panel_text(pack)}",
        _pack_menu(pack, pack_id),
    )


async def _swap(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        # Media messages cannot be edited into text; send a fresh one instead.
        await callback.message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- FSM text flow
@router.message(PackManagerStates.waiting_copy_title, F.text)
async def copy_title_received(
    message: Message, bot: Bot, db: Database, state: FSMContext, bot_username: str
) -> None:
    user = message.from_user
    if user is None:
        return
    value = (message.text or "").strip()
    if value.startswith("/"):
        await state.clear()
        await message.answer("تم إلغاء نسخ الحزمة.")
        return
    data = await state.get_data()
    pack_id = int(data.get("pack_id") or 0)
    try:
        title = normalize_title(value)
        pack = await _load_pack(bot, db, user.id, pack_id, bot_username)
        new_name = await copy_pack(bot, user.id, pack, title, bot_username)
        await db.save_active_pack(user.id, new_name, title, pack.sticker_format, auto=False)
        created = [row for row in await db.get_packs(user.id) if row.name == new_name]
        if created:
            try:
                await sync_pack(db, created[0].id, await fetch_pack(bot, new_name, bot_username))
            except PackManagerError:
                log.debug("could not mirror the copied pack locally", exc_info=True)
        await state.clear()
        await message.answer(
            f"📄 نُسخت الحزمة إلى «{html.escape(title)}».\n"
            f"https://t.me/addstickers/{new_name}"
        )
    except PackManagerError as exc:
        await message.answer(str(exc))
    except Exception:
        log.exception("Pack copy failed for %s", user.id)
        await message.answer("تعذر نسخ الحزمة.")


@router.message(PackManagerStates.waiting_rename_title, F.text)
async def rename_title_received(
    message: Message, bot: Bot, db: Database, state: FSMContext, bot_username: str
) -> None:
    user = message.from_user
    if user is None:
        return
    value = (message.text or "").strip()
    if value.startswith("/"):
        await state.clear()
        await message.answer("تم إلغاء تغيير الاسم.")
        return
    data = await state.get_data()
    pack_id = int(data.get("pack_id") or 0)
    try:
        title = normalize_title(value)
        pack = await _load_pack(bot, db, user.id, pack_id, bot_username)
        await rename_pack(bot, pack, title)
        await db.update_pack_title(pack_id, user.id, title)
        await state.clear()
        await message.answer(f"✏️ أصبح اسم الحزمة «{html.escape(title)}» في تيليجرام وسجلك.")
    except PackManagerError as exc:
        await message.answer(str(exc))
    except Exception:
        log.exception("Pack rename failed for %s", user.id)
        await message.answer("تعذر تغيير اسم الحزمة.")
