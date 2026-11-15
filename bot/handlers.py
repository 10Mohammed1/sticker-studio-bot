"""Merged handlers: conversion, sticker packs, albums, and the admin panel."""
from __future__ import annotations

import asyncio
import html
import logging
import shutil
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from . import delivery, stickers
from .ai import manager as ai_manager
from .config import settings
from .packs import auto_manager
from .converter import (
    ConversionError,
    OutputFile,
    convert_image_to_motion_webm,
    convert_image_to_webp,
    extract_video_frame,
    process_file,
)
from .database import Database
from .intake import MediaInfo, MediaIntakeError, receive_media
from .tasks.runtime import run_tracked, task_manager
from .keyboards import (
    admin_keyboard,
    main_keyboard,
    pack_editor_keyboard,
    packs_keyboard,
)

log = logging.getLogger("sticker-studio.handlers")
router = Router(name="main")

_last_activity: dict[int, float] = {}
_albums: dict[tuple[int, str], dict] = {}

MEDIA_FILTER = (
    F.photo | F.document | F.animation | F.video | F.video_note
    | F.sticker | F.audio | F.voice
)

MAX_PACK_BUTTONS = 25

WELCOME = (
    "أهلاً بك في <b>Sticker Studio</b> 👋\n\n"
    "أرسل صورة أو GIF أو فيديو أو صوتًا أو SVG أو PDF أو ZIP أو مستندًا، "
    "وأجهّز لك الملصقات جاهزة للرفع إلى @Stickers.\n\n"
    "<b>أوامر قصيرة:</b>\n"
    "/n حزمة جديدة · /e إنهاء الحزمة · /p حزمي\n"
    "/s إعدادات · /h السجل · /f لقطة من فيديو\n"
    "/m حركة من صورة · /a تحريك · /d تحرير\n"
    "/t قوالب · /v مفضلة · /g إدارة الحزم · /c إلغاء\n\n"
    "/help للتفاصيل · حد التنزيل 20 MB."
)

HELP = (
    "<b>طريقة الاستخدام</b>\n"
    "1) أرسل الملف أو صورة عادية؛ اكتشاف النوع تلقائي بالكامل.\n"
    "2) يصل الناتج كـ Document جاهز للرفع إلى @Stickers.\n"
    "3) أول صورة تُنشئ حزمتك الثابتة، وأول GIF/فيديو حزمتك المتحركة "
    "(عند تفعيل الإضافة التلقائية).\n"
    "4) عند 50 ملصقًا تُفتح حزمة إضافية تلقائيًا دون حذف القديم.\n"
    "5) الملفات المتطابقة تُكتشف بـ SHA-256 فلا تُضاف مرتين.\n\n"
    "<b>الأوامر القصيرة</b>\n"
    "/n حزمة جديدة · /e إنهاء · /p حزمي · /s إعدادات\n"
    "/f لقطة من فيديو · /m حركة · /a تحريك · /d تحرير\n"
    "/t قوالب · /v مفضلة · /g إدارة الحزم · /h السجل\n"
    "/c إلغاء · /i رقم حسابي · /k آخر المهام · /help\n\n"
    "<b>ملاحظات</b>\n"
    "• لا خلط بين WEBP وWEBM/TGS داخل حزمة واحدة (قيد تيليجرام).\n"
    "• الثابت 512KB، والفيديو 3 ثوانٍ و256KB.\n"
    "• الأوامر الطويلة القديمة تعمل أيضًا: /newpack /history /frame … إلخ.\n"
    "• المشرفون: /admin /broadcast /block /unblock."
)

PROCESSING_TEXT = "⏳ استلمت الملف، بفحصه وتجهيزه…"


class PackStates(StatesGroup):
    waiting_title = State()
    waiting_pack_rename = State()


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in settings.admin_ids)


async def is_admin_user(db: Database, user) -> bool:
    """Admin check that also honours ``ADMIN_USERNAMES``.

    A username is bound to the first numeric ID that uses it, so a later
    username squatter cannot inherit admin rights. The binding is stored in
    ``settings`` and needs no restart.
    """
    if user is None:
        return False
    if is_admin(user.id):
        return True
    username = (getattr(user, "username", None) or "").lstrip("@").lower()
    if not username or username not in settings.admin_usernames:
        return False
    key = f"admin_bind:{username}"
    bound = await db.get_setting(key)
    if bound is None:
        await db.set_setting(key, str(user.id))
        log.info("Bound admin username @%s to user %s", username, user.id)
        return True
    return bound == str(user.id)


async def is_admin_id(db: Database, user_id: int | None) -> bool:
    """True when the numeric ID is listed in ``ADMIN_IDS`` or bound to a username."""
    if not user_id:
        return False
    if is_admin(user_id):
        return True
    for username in settings.admin_usernames:
        if await db.get_setting(f"admin_bind:{username}") == str(user_id):
            return True
    return False


def _html(value: str | None) -> str:
    return html.escape(value or "", quote=False)


def _auto_pack_title(first_name: str | None) -> str | None:
    """Return an automatic pack title, or None when AUTO_PACK is disabled."""
    if not settings.auto_pack:
        return None
    return stickers.auto_pack_title(first_name)


async def _check_access(message: Message, db: Database, *, cooldown: bool = True) -> bool:
    """Touch the user and enforce block / maintenance / cooldown rules."""
    user = message.from_user
    if user is None or user.is_bot:
        return False
    await db.touch_user(user.id, user.username, user.first_name)
    if await db.is_blocked(user.id):
        await message.answer("لا يمكنك استخدام البوت حاليًا.")
        return False
    if await db.get_maintenance() and not await is_admin_user(db, user):
        await message.answer("البوت في وضع الصيانة حاليًا. حاول لاحقًا.")
        return False
    if cooldown and settings.user_cooldown_seconds:
        now = time.monotonic()
        previous = _last_activity.get(user.id, 0.0)
        if now - previous < settings.user_cooldown_seconds:
            await message.answer("انتظر لحظة قبل إرسال ملف آخر.")
            return False
        _last_activity[user.id] = now
        if len(_last_activity) > 5000:  # keep the in-memory map bounded
            cutoff = now - 3600
            for uid, seen in list(_last_activity.items()):
                if seen < cutoff:
                    _last_activity.pop(uid, None)
    return True


async def _admin_text(db: Database) -> str:
    stats = await db.get_stats()
    ai_state = "✅ مفعلة" if await ai_manager.background_enabled(db) else "➖ معطلة"
    return (
        "<b>لوحة المطور – Sticker Studio</b>\n"
        f"👥 المستخدمون: {stats.users}\n"
        f"📦 الحزم المحفوظة: {stats.packs}\n"
        f"⚙️ عمليات المعالجة: {stats.jobs}\n"
        f"⚠️ العمليات الفاشلة: {stats.failed}\n"
        f"🚫 المحظورون: {stats.blocked}\n"
        f"⏳ المهام المنتظرة/النشطة: {task_manager.active_count}\n"
        f"🧠 إزالة الخلفية: {ai_state}\n"
        f"💾 مساحة البيانات المتاحة: {shutil.disk_usage(settings.data_dir).free // (1024 ** 3)} GB"
    )


async def _admin_markup(db: Database):
    return admin_keyboard(
        await db.get_maintenance(), await ai_manager.background_enabled(db)
    )


# --------------------------------------------------------------------------
# Basic commands
# --------------------------------------------------------------------------
@router.message(CommandStart())
async def start(message: Message, db: Database) -> None:
    if not await _check_access(message, db, cooldown=False):
        return
    await message.answer(WELCOME, reply_markup=main_keyboard())


@router.message(Command("help"))
async def help_message(message: Message, db: Database) -> None:
    if not await _check_access(message, db, cooldown=False):
        return
    await message.answer(HELP, reply_markup=main_keyboard())


@router.message(Command("i", "id"))
async def user_id(message: Message, db: Database) -> None:
    if message.from_user:
        user = message.from_user
        username = f"\n@{user.username}" if user.username else ""
        admin = await is_admin_user(db, user)
        note = "\n✅ لديك صلاحية المطور." if admin else ""
        await message.answer(
            f"Telegram ID الخاص بك: <code>{user.id}</code>{_html(username)}{note}"
        )


@router.message(Command("c", "cancel"))
async def cancel_state(message: Message, state: FSMContext, db: Database) -> None:
    state_data = await state.get_data()
    for key in ("editor_session_id", "animation_session_id"):
        session_id = state_data.get(key)
        if session_id:
            try:
                await db.update_edit_session(str(session_id), "", "cancelled")
            except Exception:
                log.debug("could not mark feature session cancelled", exc_info=True)
    for key in ("editor_original", "animation_source"):
        raw = state_data.get(key)
        if raw:
            try:
                candidate = Path(raw).resolve()
                root = (settings.data_dir / "feature_sessions" / str(message.from_user.id if message.from_user else 0)).resolve()
                candidate.relative_to(root)
                shutil.rmtree(candidate.parent, ignore_errors=True)
            except (OSError, ValueError):
                pass
    await state.clear()
    cancelled = 0
    if message.from_user:
        cancelled = task_manager.cancel_user(message.from_user.id)
    if cancelled:
        await message.answer("وصل طلب إلغاء المعالجة الحالية. سيتم تنظيف الملفات المؤقتة.")
    else:
        await message.answer("تم إلغاء الحالة الحالية. أرسل /newpack لبدء حزمة جديدة.")


# --------------------------------------------------------------------------
# Sticker packs
# --------------------------------------------------------------------------
@router.message(Command("n", "newpack"))
@router.message(F.text == "📦 إنشاء حزمة")
async def new_pack(message: Message, state: FSMContext, db: Database) -> None:
    if not await _check_access(message, db, cooldown=False):
        return
    # Only /newpack carries a title; the reply button must ask for one instead
    # of turning its own label into a pack name.
    text = (message.text or "").strip()
    title = text.split(maxsplit=1)[1] if text.startswith("/newpack") and " " in text else ""
    title = stickers.normalize_pack_title(title)
    if title is None:
        await state.set_state(PackStates.waiting_title)
        await message.answer(
            "أرسل اسم الحزمة الآن (من 1 إلى 64 حرفًا).\n"
            "مثال: ملصقاتي المفضلة\n"
            "أو أرسل /cancel للإلغاء."
        )
        return
    await state.clear()
    if message.from_user is None:
        return
    await db.start_pack(message.from_user.id, title)
    await message.answer(
        f"تم تجهيز الحزمة: <b>{_html(title)}</b>\n"
        "أرسل أول صورة/GIF/فيديو، وسأُنشئ الحزمة في تيليجرام وأضيف الناتج إليها.\n\n"
        "تنبيه: كل ملصقات الحزمة يجب أن تكون بنفس الصيغة (كلها WEBP أو كلها WEBM أو كلها TGS).",
        reply_markup=main_keyboard(),
    )


@router.message(Command("mypack"))
@router.message(F.text == "📁 حزمتي")
async def my_pack(message: Message, db: Database, state: FSMContext) -> None:
    if not message.from_user:
        return
    await state.clear()
    await db.touch_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name
    )
    active = await db.get_active_pack(message.from_user.id)
    if not active.exists:
        hint = (
            "أرسل أي صورة أو فيديو وسأُنشئ حزمة تلقائيًا، أو /newpack لاختيار الاسم."
            if settings.auto_pack
            else "اكتب /newpack لبدء إنشاء واحدة."
        )
        await message.answer(f"ليس لديك حزمة نشطة. {hint}")
        return
    lines = [
        f"📦 <b>{_html(active.title)}</b>",
        f"الصيغة: {stickers.format_label(active.sticker_format)}",
    ]
    if active.name:
        lines.append(f"الرابط: {stickers.pack_link(active.name)}")
    else:
        lines.append("لم يُضف أي ملصق بعد. أرسل ملفًا لإنشاء الحزمة.")
    await message.answer("\n".join(lines))


@router.message(Command("e", "endpack"))
@router.message(F.text == "❌ إنهاء الحزمة")
async def end_pack(message: Message, db: Database, state: FSMContext) -> None:
    if not message.from_user:
        return
    await state.clear()
    active = await db.get_active_pack(message.from_user.id)
    await db.clear_active_pack(message.from_user.id)
    if active.exists:
        await message.answer(
            "تم إنهاء الحزمة النشطة. الملصقات التي أضفتها باقية في تيليجرام.\n"
            "أول ملف ترسله بعد الآن سيبدأ حزمة جديدة تلقائيًا، أو استخدم /newpack لتسميتها."
        )
    else:
        await message.answer(
            "لا توجد حزمة نشطة لإنهائها. أرسل ملفًا لإنشاء حزمة تلقائيًا، أو /newpack لتسميتها."
        )


# --------------------------------------------------------------------------
# Pack editing panel
# --------------------------------------------------------------------------
@router.message(Command("packs", "editpack"))
@router.message(F.text == "✏️ تعديل الحزمة")
async def packs_panel(message: Message, db: Database, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    # Opening the panel always cancels a pending /newpack or rename prompt;
    # otherwise the next text would silently become a pack title.
    await state.clear()
    await db.touch_user(user.id, user.username, user.first_name)
    packs = await db.get_packs(user.id)
    if not packs:
        await message.answer(
            "لا توجد حزم محفوظة بعد.\n"
            "أرسل أول صورة أو استخدم /newpack، وستظهر حزمك هنا للتعديل."
        )
        return
    note = f"\n(تظهر آخر {MAX_PACK_BUTTONS} حزمة)" if len(packs) > MAX_PACK_BUTTONS else ""
    await message.answer(
        "✏️ <b>حزمك السابقة</b>\n"
        "اختر حزمة لعرض نوعها ورابطها، أو لتغيير اسمها أو إضافة ملصقات جديدة إليها."
        + note,
        reply_markup=packs_keyboard(packs[:MAX_PACK_BUTTONS]),
    )


@router.callback_query(F.data.startswith("packs:"))
async def packs_callbacks(
    callback: CallbackQuery, db: Database, bot: Bot, state: FSMContext
) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    user = callback.from_user

    if action == "list":
        packs = await db.get_packs(user.id)
        if not packs:
            await callback.answer("لا توجد حزم محفوظة بعد.", show_alert=True)
            return
        await _edit_panel(
            callback, bot,
            "✏️ <b>حزمك السابقة</b>\nاختر حزمة:",
            packs_keyboard(packs[:MAX_PACK_BUTTONS]),
        )
        await callback.answer()
        return

    try:
        pack_id = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("طلب غير صالح.", show_alert=True)
        return

    pack = await db.get_pack(pack_id, user.id)
    if pack is None:
        await callback.answer("لم أجد هذه الحزمة.", show_alert=True)
        return

    if action == "open":
        active = await db.get_active_pack(user.id)
        status = "✅ نشطة الآن" if active.name == pack.name else "غير نشطة"
        await _edit_panel(
            callback, bot,
            f"📦 <b>{_html(pack.title)}</b>\n"
            f"النوع: {stickers.format_label(pack.sticker_format)}\n"
            f"الحالة: {status}\n"
            f"{stickers.pack_link(pack.name)}",
            pack_editor_keyboard(pack),
        )
    elif action == "rename":
        await state.set_state(PackStates.waiting_pack_rename)
        await state.update_data(pack_id=pack.id)
        await _callback_send(
            callback, bot,
            f"أرسل الاسم الجديد للحزمة <b>{_html(pack.title)}</b> (من 1 إلى 64 حرفًا).\n"
            "أو /cancel للإلغاء.",
        )
    elif action == "use":
        await db.activate_pack(user.id, pack)
        await _callback_send(
            callback, bot,
            f"📦 أصبحت حزمة <b>{_html(pack.title)}</b> نشطة.\n"
            f"النوع: {stickers.format_label(pack.sticker_format)}\n"
            "أرسل الملصقات الآن وستُضاف إليها.",
        )
    # Telegram rejects a second answer() for the same callback, so this must be
    # the only acknowledgement on every path above.
    await callback.answer()


@router.message(F.text == "🔄 تحويل تلقائي")
async def conversion_hint(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "أرسل الملف هنا مباشرة. سأكتشف صيغته الحقيقية، أجهّزه كملصق تيليجرام، "
        "وأضيفه تلقائيًا إلى حزمتك.\n"
        "استخدم ⚙️ إعدادات الحزم لإيقاف الإضافة التلقائية مع استمرار التحويل."
    )


# --------------------------------------------------------------------------
# Permanent packs (4.1): two sets per user, reused forever
# --------------------------------------------------------------------------
@router.message(Command("send"))
@router.message(F.text == "📷 إرسال صورة أو ملف للتحويل")
async def send_prompt(message: Message, db: Database, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await db.touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "📷 <b>أرسل الملف الآن</b>\n"
        "• صورة ← ملصق ثابت WEBP في حزمتك الثابتة\n"
        "• GIF أو فيديو ← ملصق متحرك WEBM في حزمتك المتحركة\n"
        "• ملصق مدعوم ← يبقى بصيغته إن كان صالحًا\n\n"
        "الاكتشاف تلقائي بالكامل: لا تحتاج لاختيار نوع التحويل.",
        reply_markup=main_keyboard(),
    )


@router.message(Command("p", "mypacks", "packsettings"))
@router.message(F.text == "⚙️ إعدادات الحزم")
async def pack_settings(message: Message, db: Database, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await db.touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text, keyboard = await auto_manager.settings_view(db, message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("h", "history"))
@router.message(F.text == "📚 سجل التحويلات")
async def conversion_history(message: Message, db: Database, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await message.answer(await auto_manager.history_text(db, message.from_user.id))


@router.message(F.text == "📦 حزمتـي الثابتة")
async def static_packs(message: Message, db: Database, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await db.touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text, keyboard = await auto_manager.packs_view(db, message.from_user.id, "static")
    await message.answer(text, reply_markup=keyboard)


@router.message(F.text == "🎞️ حزمتـي المتحركة")
async def animated_packs(message: Message, db: Database, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await db.touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text, keyboard = await auto_manager.packs_view(db, message.from_user.id, "animated")
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("autopack:"))
async def autopack_callbacks(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    user = callback.from_user
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    argument = parts[2] if len(parts) > 2 else ""

    if action == "off":
        if not settings.auto_pack:
            await callback.answer("الإضافة التلقائية معطلة من إعدادات الاستضافة.", show_alert=True)
            return
        await auto_manager.set_enabled(db, user.id, False)
        text, keyboard = await auto_manager.settings_view(db, user.id)
        if callback.message:
            try:
                await callback.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                await _callback_send(callback, bot, text)
        await callback.answer("🔴 أوقفت الإضافة التلقائية. التحويل ما زال يعمل.")
        return
    if action == "on":
        await auto_manager.set_enabled(db, user.id, True)
        text, keyboard = await auto_manager.settings_view(db, user.id)
        if callback.message:
            try:
                await callback.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                await _callback_send(callback, bot, text)
        await callback.answer("🟢 فعّلت الإضافة التلقائية.")
        return
    if action == "view":
        text, keyboard = await auto_manager.packs_view(db, user.id, argument or "static")
        await _edit_panel(callback, bot, text, keyboard)
        await callback.answer()
        return
    if action == "history":
        await _callback_send(callback, bot, await auto_manager.history_text(db, user.id))
        await callback.answer()
        return
    if action == "settings":
        text, keyboard = await auto_manager.settings_view(db, user.id)
        await _edit_panel(callback, bot, text, keyboard)
        await callback.answer()
        return
    if action == "refresh":
        text, keyboard = await auto_manager.settings_view(db, user.id)
        await _edit_panel(callback, bot, text, keyboard)
        await callback.answer("تم التحديث.")
        return
    await callback.answer("إجراء غير معروف.", show_alert=True)


# --------------------------------------------------------------------------
# Admin panel
# --------------------------------------------------------------------------
@router.message(Command("admin"))
async def admin_panel(message: Message, db: Database) -> None:
    user = message.from_user
    if user is None or not await is_admin_user(db, user):
        await message.answer("هذا الأمر للمطور فقط.")
        return
    await db.touch_user(user.id, user.username, user.first_name)
    await message.answer(await _admin_text(db), reply_markup=await _admin_markup(db))


@router.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not await is_admin_user(db, callback.from_user):
        await callback.answer("غير مصرّح", show_alert=True)
        return
    action = (callback.data or "").partition(":")[2]
    target = callback.message

    if action == "stats":
        await _callback_send(callback, bot, await _admin_text(db))
    elif action == "users":
        rows = await db.get_users(15)
        if not rows:
            text = "لا يوجد مستخدمون بعد."
        else:
            lines = ["<b>آخر المستخدمين</b>"]
            for row in rows:
                username = f"@{row.username}" if row.username else "بدون_اسم"
                pack = f" | 📦 {_html(row.pack_title)}" if row.pack_title else ""
                state = "🚫 محظور" if row.blocked else "✅ نشط"
                lines.append(
                    f"<code>{row.user_id}</code> | {_html(username)[:32]} | "
                    f"{_html(row.first_name or '—')[:32]} | {state}{pack[:48]}"
                )
            text = "\n".join(lines)
        await _callback_send(callback, bot, text)
    elif action == "monitor":
        from .admin.monitoring import collect, format_report
        from .tasks.runtime import job_queue

        snapshot = await collect(db, job_queue, settings.data_dir)
        await _callback_send(callback, bot, format_report(snapshot))
    elif action == "ai":
        enabled = not await ai_manager.background_enabled(db)
        await ai_manager.set_background(db, enabled)
        await _callback_send(callback, bot, ai_manager.capabilities_report())
        await callback.answer("تم التحديث")
        return
    elif action == "errors":
        rows = await db.recent_failed_jobs(10)
        if not rows:
            text = "✅ لا توجد عمليات فاشلة مسجلة."
        else:
            lines = ["<b>آخر العمليات الفاشلة</b>"]
            for row in rows:
                detail = _html(str(row["detail"] or ""))[:90]
                lines.append(
                    f"• <code>{str(row['task_id'])[:10]}</code> "
                    f"{_html(str(row['task_type']))[:24]} — {detail}"
                )
            text = "\n".join(lines)
        await _callback_send(callback, bot, text)
    elif action == "backup":
        await callback.answer("جارٍ إنشاء النسخة الاحتياطية…")
        try:
            path = await _backup_database(db)
            await bot.send_document(
                callback.from_user.id,
                FSInputFile(path, filename="sticker-studio-backup.sqlite3"),
                caption="📥 نسخة احتياطية متسقة من قاعدة البيانات.",
            )
        except Exception:
            log.exception("admin backup failed")
            await bot.send_message(callback.from_user.id, "تعذر إنشاء النسخة الاحتياطية.")
        finally:
            shutil.rmtree(Path(settings.work_dir) / "backups", ignore_errors=True)
        return
    elif action == "maintenance":
        enabled = not await db.get_maintenance()
        await db.set_maintenance(enabled)
        if isinstance(target, Message):
            try:
                await target.edit_text(
                    await _admin_text(db), reply_markup=await _admin_markup(db)
                )
            except TelegramBadRequest:
                pass
        else:
            await bot.send_message(
                callback.from_user.id,
                await _admin_text(db),
                reply_markup=await _admin_markup(db),
            )
        await callback.answer(
            "تم تفعيل وضع الصيانة" if enabled else "تم إيقاف وضع الصيانة"
        )
        return
    elif action == "refresh":
        if isinstance(target, Message):
            try:
                await target.edit_text(
                    await _admin_text(db),
                    reply_markup=await _admin_markup(db),
                )
            except TelegramBadRequest:
                pass
        else:
            await bot.send_message(
                callback.from_user.id,
                await _admin_text(db),
                reply_markup=await _admin_markup(db),
            )
    await callback.answer()


async def _backup_database(db: Database) -> Path:
    """Consistent SQLite copy built with the online backup API."""
    import sqlite3

    folder = Path(settings.work_dir) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"sticker-studio-{uuid.uuid4().hex[:8]}.sqlite3"

    def copy() -> None:
        source = sqlite3.connect(db.path)
        try:
            target = sqlite3.connect(destination)
            try:
                with target:
                    source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    await asyncio.to_thread(copy)
    return destination


async def _callback_send(callback: CallbackQuery, bot: Bot, text: str) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.answer(text)
            return
        except Exception:
            log.debug("could not answer via callback message", exc_info=True)
    await bot.send_message(callback.from_user.id, text)


async def _edit_panel(
    callback: CallbackQuery, bot: Bot, text: str, markup=None
) -> None:
    """Update an inline panel in place, falling back to a fresh message."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.debug("could not edit pack panel", exc_info=True)
    await bot.send_message(callback.from_user.id, text, reply_markup=markup)


BROADCAST_CHUNK = 4000


def split_message(text: str, size: int = BROADCAST_CHUNK) -> list[str]:
    """Split long text on paragraph boundaries so Telegram accepts every part."""
    chunks: list[str] = []
    rest = text.strip()
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


async def _deliver_broadcast(bot: Bot, user_id: int, chunks: list[str]) -> str:
    """Send every chunk to one recipient; return ok | blocked | failed."""
    for piece in chunks:
        try:
            await bot.send_message(user_id, piece, parse_mode=None)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1.0)
            try:
                await bot.send_message(user_id, piece, parse_mode=None)
            except TelegramForbiddenError:
                return "blocked"
            except Exception:
                return "failed"
        except TelegramForbiddenError:
            return "blocked"
        except Exception:
            return "failed"
    return "ok"


@router.message(Command("broadcast"))
async def broadcast(message: Message, db: Database, bot: Bot) -> None:
    if message.from_user is None or not await is_admin_user(db, message.from_user):
        await message.answer("هذا الأمر للمطور فقط.")
        return
    parts = (message.text or "").split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        await message.answer("استخدم: /broadcast نص الرسالة")
        return
    chunks = split_message(text)
    user_ids = await db.all_user_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        outcome = await _deliver_broadcast(bot, uid, chunks)
        if outcome == "ok":
            sent += 1
        else:
            failed += 1
            if outcome == "blocked":
                await db.set_blocked(uid, True)
        await asyncio.sleep(0.05)
    await message.answer(
        f"انتهى الإرسال إلى {len(user_ids)} مستخدمًا.\n"
        f"ناجح: {sent} | تعذر: {failed}"
    )


@router.message(Command("block"))
async def block_user(message: Message, db: Database) -> None:
    if message.from_user is None or not await is_admin_user(db, message.from_user):
        await message.answer("هذا الأمر للمطور فقط.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("استخدم: /block USER_ID")
        return
    target = int(parts[1])
    if await is_admin_id(db, target):
        await message.answer("لا يمكن حظر مشرف.")
        return
    if await db.set_blocked(target, True):
        await message.answer("تم حظر المستخدم من استخدام البوت.")
    else:
        await message.answer("لا يوجد مستخدم بهذا الرقم في قاعدة البيانات.")


@router.message(Command("unblock"))
async def unblock_user(message: Message, db: Database) -> None:
    if message.from_user is None or not await is_admin_user(db, message.from_user):
        await message.answer("هذا الأمر للمطور فقط.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("استخدم: /unblock USER_ID")
        return
    target = int(parts[1])
    if await db.set_blocked(target, False):
        await message.answer("تم إلغاء حظر المستخدم.")
    else:
        await message.answer("لا يوجد مستخدم بهذا الرقم في قاعدة البيانات.")


# --------------------------------------------------------------------------
# Album buffering
# --------------------------------------------------------------------------
@router.message(MEDIA_FILTER)
async def incoming_media(message: Message, bot: Bot, db: Database, bot_username: str) -> None:
    if message.from_user is None:
        return

    if message.media_group_id:
        key = (message.chat.id, message.media_group_id)
        bucket = _albums.get(key)
        if bucket is None:
            if not await _check_access(message, db):
                return
            bucket = {"messages": [], "task": None}
            _albums[key] = bucket
        bucket["messages"].append(message)
        # Restart the idle timer on every new item: an album with slow delivery
        # must not be split into several mini-batches.
        previous = bucket.get("task")
        if previous is not None and not previous.done():
            previous.cancel()
        bucket["task"] = asyncio.create_task(_flush_album(key, bot, db, bot_username))
        return

    if not await _check_access(message, db):
        return
    await _process_messages([message], bot, db, bot_username)


ALBUM_IDLE_SECONDS = 1.2


async def _flush_album(
    key: tuple[int, str], bot: Bot, db: Database, bot_username: str
) -> None:
    # Telegram sends album items as separate updates with the same media_group_id.
    # Each new item cancels this task and schedules a new one, so this sleep only
    # ends after the album has been idle for the full window.
    try:
        await asyncio.sleep(ALBUM_IDLE_SECONDS)
    except asyncio.CancelledError:
        return
    bucket = _albums.pop(key, None)
    if bucket and bucket["messages"]:
        await _process_messages(bucket["messages"], bot, db, bot_username)


# --------------------------------------------------------------------------
# Shared conversion pipeline
# --------------------------------------------------------------------------
async def _process_messages(
    messages: list[Message], bot: Bot, db: Database, bot_username: str
) -> None:
    if not messages or messages[0].from_user is None:
        return
    user_id = messages[0].from_user.id
    task_id = uuid.uuid4().hex[:16]
    await db.create_task(task_id, user_id, "media_conversion")
    try:
        async with task_manager.slot(user_id):
            await db.update_task(task_id, "running", 5, "بدأت معالجة الملفات")
            succeeded = await _process_messages_impl(messages, bot, db, bot_username, task_id)
        await db.update_task(
            task_id, "completed" if succeeded else "failed",
            100 if succeeded else 0,
            "انتهت المعالجة" if succeeded else "تعذر تحويل أو تسليم الملفات",
        )
    except asyncio.CancelledError:
        await db.update_task(task_id, "cancelled", 0, "ألغى المستخدم المهمة")
        raise
    except Exception:
        await db.update_task(task_id, "failed", 0, "فشل غير متوقع")
        log.exception("[%s] task wrapper failed", task_id)


async def _process_messages_impl(
    messages: list[Message], bot: Bot, db: Database, bot_username: str,
    task_id: str | None = None,
) -> bool:
    if not messages:
        return False
    first = messages[0]
    user = first.from_user
    if user is None:
        return False
    trace = delivery.new_trace_id()
    log.info("[%s] processing %d incoming message(s)", trace, len(messages))

    status = None
    try:
        status = await first.reply(PROCESSING_TEXT)
    except Exception:
        log.warning("[%s] could not send the acknowledgement message", trace, exc_info=True)

    work: Path | None = None
    try:
        work = Path(tempfile.mkdtemp(prefix="sticker-studio-", dir=settings.work_dir))
        outputs: list[OutputFile] = []
        errors: list[str] = []
        if len(messages) > settings.max_archive_items:
            extra = len(messages) - settings.max_archive_items
            errors.append(
                f"تخطيت {extra} ملفًا إضافيًا في الألبوم (الحد {settings.max_archive_items})."
            )
        seen_uploads: set[str] = set()

        for index, message in enumerate(messages[: settings.max_archive_items], start=1):
            item_trace = f"{trace}.{index}"
            try:
                try:
                    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT)
                except Exception:
                    log.debug("[%s] chat action failed (ignored)", item_trace)

                item_dir = work / f"input_{index:02d}"
                output_dir = work / f"output_{index:02d}"
                media: MediaInfo = await receive_media(
                    message, bot, item_dir, settings, item_trace
                )
                if media.sha256 in seen_uploads:
                    log.info("[%s] duplicate upload skipped", item_trace)
                    errors.append("تخطيت ملفًا مكررًا داخل نفس الدفعة.")
                    continue
                seen_uploads.add(media.sha256)

                log.info(
                    "[%s] converting %s (%s, %s, %d bytes)",
                    item_trace, media.name, media.telegram_kind, media.mime, media.size,
                )
                preferences = await db.get_user_preferences(user.id)
                user_settings = replace(
                    settings,
                    enable_bg_removal=settings.enable_bg_removal and bool(preferences["bg_removal"]),
                )
                try:
                    results = await asyncio.to_thread(
                        process_file, media.path, media.name, output_dir, user_settings,
                        telegram_kind=media.telegram_kind,
                    )
                except ConversionError:
                    if media.telegram_kind != "video_note":
                        raise
                    # A video note that cannot become WEBM still yields a usable static sticker.
                    log.info("[%s] video note -> WEBM failed; extracting a frame", item_trace)
                    frame_path = output_dir / "video_note_frame.png"
                    await asyncio.to_thread(
                        extract_video_frame, media.path, frame_path,
                        settings.ffmpeg_path, 0.1, settings.max_processing_seconds,
                    )
                    frame_webp = output_dir / "video_note.webp"
                    frame_info = await asyncio.to_thread(
                        convert_image_to_webp, frame_path, frame_webp,
                        settings.enable_bg_removal and bool(preferences["bg_removal"]),
                    )
                    results = [
                        OutputFile(
                            frame_webp,
                            "WEBP",
                            f"لقطة ثابتة من فيديو نوت — 512×512 — "
                            f"{frame_info['size'] // 1024} KB",
                        )
                    ]

                # Album items often share the same Telegram-generated filename.
                for result_index, result in enumerate(results, start=1):
                    unique_path = result.path.with_name(
                        f"{index:02d}_{result_index:02d}_{result.path.name}"
                    )
                    if unique_path != result.path:
                        result.path.replace(unique_path)
                        result.path = unique_path
                outputs.extend(results)
                formats = "+".join(sorted({item.kind for item in results}))
                await db.add_job(user.id, media.kind, formats, "success")
                log.info(
                    "[%s] conversion produced %d file(s)", item_trace, len(results)
                )
            except MediaIntakeError as exc:
                log.warning("[%s] intake failed: %s", item_trace, exc)
                errors.append(str(exc))
                await db.add_job(user.id, "unknown", "none", "failed")
            except ConversionError as exc:
                log.warning("[%s] conversion failed: %s", item_trace, exc)
                errors.append(str(exc).splitlines()[0])
                await db.add_job(user.id, "unknown", "none", "failed")
            except Exception:
                log.exception("[%s] unexpected error while processing incoming media", item_trace)
                errors.append(f"خطأ غير متوقع أثناء معالجة الملف (رقم التتبع: {item_trace}).")
                await db.add_job(user.id, "unknown", "none", "failed")
            if task_id:
                progress = 5 + int(index / max(1, min(len(messages), settings.max_archive_items)) * 80)
                await db.update_task(task_id, "running", progress, f"معالجة الملف {index}")

        if not outputs:
            reason = errors[0] if errors else "لم أجد ملفًا مدعومًا للتحويل."
            await delivery.notify(
                bot, first, status,
                f"❌ لم أستطع تجهيز الملف:\n{_html(reason)}\n\n"
                f"جرّب ملفًا آخر أو صيغة مختلفة.\nرقم التتبع: {trace}",
            )
            return False

        await delivery.edit_status(status, "✅ التحويل اكتمل، جاري إرسال النتيجة…")
        try:
            await delivery.send_results(
                bot, first.chat.id, outputs, work, trace, first.message_id
            )
        except delivery.DeliveryError as exc:
            log.exception("[%s] delivery failed", trace)
            await delivery.notify(
                bot, first, status,
                f"✅ اكتمل التحويل لكن تعذر إرسال الملف:\n{_html(str(exc))}\n\nرقم التتبع: {trace}",
            )
            return False

        # Pack attachments happen after delivery so a slow Telegram call never
        # delays the converted file the user is waiting for.
        auto_on = await auto_manager.enabled(db, user.id)
        outcomes = await auto_manager.handle_outputs(
            bot, db, user.id, outputs,
            bot_username=bot_username,
            first_name=user.first_name,
            task_id=task_id,
            source_kind=messages[0].content_type or "file",
        )
        summary = auto_manager.summary_text(
            outcomes, converted=len(outputs), auto_enabled=auto_on
        )
        if summary:
            await _send_auto_summary(
                first, bot, summary, auto_manager.summary_keyboard(outcomes)
            )

        if errors:
            note = "ملاحظة: لم أستطع معالجة بعض الملفات:\n" + "\n".join(
                f"• {_html(item)}" for item in errors[:5]
            )
            try:
                await first.reply(note + f"\n\nرقم التتبع: {trace}")
            except Exception:
                log.warning("[%s] could not send the partial-failure note", trace)
        await delivery.delete_status(status)
        return True
    except Exception:
        log.exception("[%s] unexpected top-level processing error", trace)
        await delivery.notify(
            bot, first, status,
            "❌ حصل خطأ غير متوقع أثناء تجهيز الملف.\n"
            "جرّب ملفًا أصغر أو أعد المحاولة.\n"
            f"\nرقم التتبع: {trace}",
        )
        return False
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


async def _send_auto_summary(
    first: Message, bot: Bot, text: str, keyboard: Any = None
) -> None:
    """Send the short conversion + pack summary with optional pack links."""
    try:
        await first.reply(text, reply_markup=keyboard)
    except Exception:
        log.warning("could not send the auto-pack summary", exc_info=True)


async def _send_notes(first: Message, bot: Bot, notes: list[str]) -> None:
    # Album items can produce the same pack warning repeatedly; send it once.
    unique: list[str] = []
    seen: set[str] = set()
    for note in notes:
        if note and note not in seen:
            seen.add(note)
            unique.append(note)
    chunk = ""
    for note in unique:
        addition = note if not chunk else "\n\n" + note
        if len(chunk) + len(addition) > 3800:
            try:
                await first.reply(chunk)
            except Exception:
                log.warning("could not send pack note", exc_info=True)
            chunk = note
        else:
            chunk += addition
    if chunk:
        try:
            await first.reply(chunk)
        except Exception:
            log.warning("could not send pack note", exc_info=True)


# --------------------------------------------------------------------------
# /frame and /motion
# --------------------------------------------------------------------------
@router.message(Command("f", "frame"))
async def frame_command(message: Message, bot: Bot, db: Database, bot_username: str) -> None:
    if not await _check_access(message, db, cooldown=False):
        return
    target = message.reply_to_message
    if target is None:
        await message.answer("استخدم /frame ردًا على فيديو أو GIF لاستخراج أول لقطة منه.")
        return
    trace = delivery.new_trace_id()
    status = None
    work: Path | None = None
    try:
        status = await message.reply("⏳ بستخرج لقطة من الفيديو…")
        work = Path(tempfile.mkdtemp(prefix="sticker-frame-", dir=settings.work_dir))
        media = await receive_media(target, bot, work, settings, trace)
        if media.kind not in {"video", "animation"}:
            raise ConversionError("هذا الملف ليس فيديو أو GIF؛ أرسل /frame ردًا على فيديو.")
        frame_path = work / "frame.png"
        await run_tracked(
            db, message.from_user.id if message.from_user else message.chat.id,
            "frame_extract", extract_video_frame, media.path, frame_path,
            settings.ffmpeg_path, 0.3, settings.max_processing_seconds,
        )
        output = work / "frame.webp"
        preferences = (
            await db.get_user_preferences(message.from_user.id)
            if message.from_user else {"bg_removal": False, "auto_pack": False}
        )
        info = await run_tracked(
            db, message.from_user.id if message.from_user else message.chat.id,
            "frame_convert", convert_image_to_webp, frame_path, output,
            settings.enable_bg_removal and bool(preferences["bg_removal"]),
        )
        await delivery.send_document(
            bot, message.chat.id, output,
            f"✅ لقطة ثابتة جاهزة — WEBP — 512×512 — {info['size'] // 1024} KB",
            trace, message.message_id,
        )
        if message.from_user:
            auto_on = await auto_manager.enabled(db, message.from_user.id)
            outcomes = await auto_manager.handle_outputs(
                bot, db, message.from_user.id, [OutputFile(output, "WEBP", "")],
                bot_username=bot_username,
                first_name=message.from_user.first_name,
                source_kind="frame",
            )
            summary = auto_manager.summary_text(
                outcomes, converted=1, auto_enabled=auto_on
            )
            if summary:
                await _send_auto_summary(
                    message, bot, summary, auto_manager.summary_keyboard(outcomes)
                )
        await delivery.delete_status(status)
    except (MediaIntakeError, ConversionError) as exc:
        await delivery.notify(bot, message, status, f"❌ {_html(str(exc))}\n\nرقم التتبع: {trace}")
    except delivery.DeliveryError as exc:
        await delivery.notify(bot, message, status, f"❌ {_html(str(exc))}\n\nرقم التتبع: {trace}")
    except Exception:
        log.exception("[%s] unexpected error in /frame", trace)
        await delivery.notify(
            bot, message, status,
            f"❌ تعذّر استخراج اللقطة من هذا الملف.\n\nرقم التتبع: {trace}",
        )
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


@router.message(Command("m", "motion"))
async def motion_command(message: Message, bot: Bot, db: Database, bot_username: str) -> None:
    if not await _check_access(message, db, cooldown=False):
        return
    target = message.reply_to_message
    if target is None:
        await message.answer("استخدم /motion ردًا على صورة لتحويلها إلى WEBM بحركة تكبير خفيفة.")
        return
    trace = delivery.new_trace_id()
    status = None
    work: Path | None = None
    try:
        status = await message.reply("⏳ بحوّل الصورة إلى ملصق فيديو…")
        work = Path(tempfile.mkdtemp(prefix="sticker-motion-", dir=settings.work_dir))
        media = await receive_media(target, bot, work, settings, trace)
        if media.kind != "image":
            raise ConversionError("هذا الملف ليس صورة ثابتة. أرسل /motion ردًا على صورة.")
        output = work / "motion.webm"
        info = await run_tracked(
            db, message.from_user.id if message.from_user else message.chat.id,
            "image_motion", convert_image_to_motion_webm, media.path, output,
            settings.ffmpeg_path, settings.max_processing_seconds, settings.ffprobe_path,
        )
        await delivery.send_document(
            bot, message.chat.id, output,
            f"✅ WEBM — 512×512 — {info['duration']:.1f} ث — "
            f"{info['size'] // 1024} KB",
            trace, message.message_id,
        )
        if message.from_user:
            auto_on = await auto_manager.enabled(db, message.from_user.id)
            outcomes = await auto_manager.handle_outputs(
                bot, db, message.from_user.id, [OutputFile(output, "WEBM", "")],
                bot_username=bot_username,
                first_name=message.from_user.first_name,
                source_kind="motion",
            )
            summary = auto_manager.summary_text(
                outcomes, converted=1, auto_enabled=auto_on
            )
            if summary:
                await _send_auto_summary(
                    message, bot, summary, auto_manager.summary_keyboard(outcomes)
                )
        await delivery.delete_status(status)
    except (MediaIntakeError, ConversionError) as exc:
        await delivery.notify(bot, message, status, f"❌ {_html(str(exc))}\n\nرقم التتبع: {trace}")
    except delivery.DeliveryError as exc:
        await delivery.notify(bot, message, status, f"❌ {_html(str(exc))}\n\nرقم التتبع: {trace}")
    except Exception:
        log.exception("[%s] unexpected error in /motion", trace)
        await delivery.notify(
            bot, message, status,
            f"❌ تعذّر تحويل الصورة إلى حركة.\n\nرقم التتبع: {trace}",
        )
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


# Registered after the named buttons so a button never becomes a pack name.
@router.message(PackStates.waiting_pack_rename, F.text)
async def rename_pack(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    user = message.from_user
    if user is None:
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("أُلغيت إعادة التسمية. استخدم /packs للعودة إلى الحزم.")
        return
    data = await state.get_data()
    try:
        pack_id = int(data.get("pack_id") or 0)
    except (TypeError, ValueError):
        pack_id = 0
    pack = await db.get_pack(pack_id, user.id) if pack_id else None
    if pack is None:
        await state.clear()
        await message.answer("لم أجد الحزمة. استخدم /packs من جديد.")
        return
    title = stickers.normalize_pack_title(text)
    if title is None:
        await message.answer(
            "الاسم يجب أن يكون بين 1 و64 حرفًا. أرسل اسمًا آخر أو /cancel للإلغاء."
        )
        return
    try:
        await bot.set_sticker_set_title(pack.name, title)
    except TelegramBadRequest as exc:
        await message.answer(
            "❌ تعذّر تغيير اسم الحزمة في تيليجرام.\n"
            f"السبب: {_html(str(exc)[:250])}"
        )
        return
    await db.update_pack_title(pack.id, user.id, title)
    await state.clear()
    await message.answer(
        f"✅ تم تغيير اسم الحزمة إلى <b>{_html(title)}</b>.\n"
        f"{stickers.pack_link(pack.name)}",
        reply_markup=main_keyboard(),
    )


# Registered after the named buttons so a button never becomes a pack title.
@router.message(PackStates.waiting_title, F.text)
async def set_pack_title(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("أُلغيت العملية. أرسل /newpack لبدء حزمة جديدة.")
        return
    title = stickers.normalize_pack_title(text)
    if title is None:
        await message.answer("اسم الحزمة يجب أن يكون بين 1 و64 حرفًا. أرسل اسمًا آخر أو /cancel.")
        return
    if message.from_user is None:
        await state.clear()
        return
    await db.start_pack(message.from_user.id, title)
    await state.clear()
    await message.answer(
        f"تم تجهيز الحزمة: <b>{_html(title)}</b>\n"
        "أرسل أول صورة/GIF/فيديو لإنشائها في تيليجرام.",
        reply_markup=main_keyboard(),
    )


# --------------------------------------------------------------------------
# Fallback text
# --------------------------------------------------------------------------
@router.message(F.text)
async def text_message(message: Message) -> None:
    if (message.text or "").startswith("/"):
        await message.answer("أمر غير معروف. اكتب /help لعرض الأوامر المتاحة.")
        return
    await message.answer(
        "أرسل صورة أو فيديو أو ملفًا مدعومًا مباشرة، وسأجهّزه لك.\n"
        "اكتب /help للمساعدة، أو /newpack لإنشاء حزمة."
    )
