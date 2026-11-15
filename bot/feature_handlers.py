"""Sticker Studio 4.0 feature routes: editor, animation, preferences and library."""
from __future__ import annotations

import asyncio
import html
import logging
import shutil
import tempfile
import uuid
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from . import delivery, stickers
from .ai import manager as ai_manager
from .ai.background import AIUnavailable, remove_background
from .ai.light import LightAIError, describe_file, run_light_tool
from .animation.effects import (
    EFFECT_LABELS,
    AnimationError,
    clamp_duration,
    clamp_speed,
    extract_frame,
    render_effect,
)
from .config import settings
from .converter import OutputFile
from .database import Database
from .editor.image_editor import EditorError, edit_image
from .intake import MediaIntakeError, receive_media
from .packs import auto_manager
from .packs.exporter import PackExportError, export_pack
from .tasks.runtime import run_tracked
from .user import templates
from .user.templates import TemplateError

log = logging.getLogger("sticker-studio.features")
router = Router(name="sticker-studio-4-features")


class FeatureStates(StatesGroup):
    editing = State()
    waiting_editor_text = State()
    waiting_template_title = State()
    animating = State()


def _safe_session_path(user_id: int, raw: str | None) -> Path | None:
    if not raw:
        return None
    root = (settings.data_dir / "feature_sessions" / str(user_id)).resolve()
    path = Path(raw).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


EDITOR_TOOL_PAGES: list[list[tuple[str, str]]] = [
    [
        ("✂️ قص مربع", "crop"),
        ("🖼 قص 4:5", "crop_45"),
        ("🖥 قص 16:9", "crop_169"),
        ("🔄 تدوير", "rotate"),
        ("🔃 قلب أفقي", "flip_h"),
        ("🔁 قلب رأسي", "flip_v"),
        ("📐 ضبط المقاس", "resize"),
        ("🤍 حدود", "outline"),
        ("🌑 ظل", "shadow"),
        ("🌫 تمويه", "blur"),
        ("✨ تحسين الحدة", "sharpen"),
        ("⚫ أبيض وأسود", "grayscale"),
        ("🟤 سيبيا", "sepia"),
    ],
    [
        ("🔤 نص", "text"),
        ("🔠 نص كبير", "text_big"),
        ("💧 علامة مائية", "watermark"),
        ("☀️ سطوع +", "brightness_up"),
        ("🌘 سطوع −", "brightness_down"),
        ("🌗 تباين +", "contrast_up"),
        ("🌈 تشبع +", "saturation_up"),
        ("🎨 خلفية بيضاء", "bg:white"),
        ("⬛ خلفية سوداء", "bg:black"),
        ("🟦 خلفية زرقاء", "bg:blue"),
    ],
    [
        ("🧠 تحليل الصورة", "ai_analyze"),
        ("🎯 قص تلقائي AI", "ai_auto"),
        ("✨ تحسين تلقائي AI", "ai_enhance"),
        ("🖼 تكبير ذكي 2×", "ai_upscale"),
        ("🧹 إزالة الخلفية AI", "removebg"),
    ],
]

ANIMATION_EFFECTS: list[tuple[str, str]] = [
    ("🔍 تكبير", "zoom_in"),
    ("🔎 تصغير", "zoom_out"),
    ("🫨 اهتزاز", "shake"),
    ("✨ نبض", "pulse"),
    ("⬅️ انزلاق", "slide_left"),
    ("➡️ انزلاق", "slide_right"),
    ("⬆️ انزلاق", "slide_up"),
    ("⬇️ انزلاق", "slide_down"),
    ("🌀 تمايل", "rotate"),
    ("🌅 ظهور", "fade_in"),
    ("🌇 اختفاء", "fade_out"),
    ("🎬 ظهور+اختفاء", "fade"),
]


def _editor_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    tools = EDITOR_TOOL_PAGES[page % len(EDITOR_TOOL_PAGES)]
    rows = [
        [
            InlineKeyboardButton(text=label, callback_data=f"editor:{callback}")
            for label, callback in tools[index:index + 2]
        ]
        for index in range(0, len(tools), 2)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=f"🎚️ الأدوات {page + 1}/{len(EDITOR_TOOL_PAGES)} — تبديل",
                callback_data=f"editor:page:{(page + 1) % len(EDITOR_TOOL_PAGES)}",
            ),
            InlineKeyboardButton(text="↩️ تراجع", callback_data="editor:undo"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="💾 حفظ كقالب", callback_data="editor:save_tpl"),
            InlineKeyboardButton(text="📂 قوالبي", callback_data="editor:tplist"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="🔙 للأصل", callback_data="editor:reset"),
            InlineKeyboardButton(text="✅ إنهاء", callback_data="editor:finish"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _animation_keyboard(duration: float = 3.0, speed: float = 1.0) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=label, callback_data=f"animate:{callback}")
            for label, callback in ANIMATION_EFFECTS[index:index + 3]
        ]
        for index in range(0, len(ANIMATION_EFFECTS), 3)
    ]
    durations = [
        InlineKeyboardButton(
            text=("✅ " if abs(duration - value) < 0.01 else "") + f"{value:g}s",
            callback_data=f"animate:set:{value:g}",
        )
        for value in (1.0, 2.0, 3.0)
    ]
    speeds = [
        InlineKeyboardButton(
            text=("✅ " if abs(speed - value) < 0.01 else "") + f"{value:g}×",
            callback_data=f"animate:speed:{value:g}",
        )
        for value in (0.5, 1.0, 2.0)
    ]
    rows.append(durations)
    rows.append(speeds)
    rows.append([InlineKeyboardButton(text="✖️ إغلاق", callback_data="animate:finish")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _preferences_keyboard(prefs: dict[str, object]) -> InlineKeyboardMarkup:
    bg = "🔒 غير متاحة على الاستضافة" if not settings.enable_bg_removal else (
        "✅ إزالة الخلفية تلقائيًا" if prefs["bg_removal"] else "❌ إزالة الخلفية تلقائيًا"
    )
    auto = "🔒 الإضافة التلقائية للحزم معطلة على الاستضافة" if not settings.auto_pack else (
        "✅ الإضافة التلقائية للحزم" if prefs["auto_pack"] else "❌ الإضافة التلقائية للحزم"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=bg, callback_data="prefs:bg")],
        [InlineKeyboardButton(text=auto, callback_data="prefs:auto")],
        [InlineKeyboardButton(text="🔄 تحديث", callback_data="prefs:refresh")],
    ])


async def _copy_replied_image(message: Message, bot: Bot, user_id: int, kind: str) -> Path:
    target = message.reply_to_message
    if target is None:
        raise ValueError("لازم ترد على صورة بالأمر.")
    session_dir = settings.data_dir / "feature_sessions" / str(user_id) / uuid.uuid4().hex
    session_dir.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="feature-intake-", dir=settings.work_dir))
    try:
        media = await receive_media(target, bot, temp, settings, uuid.uuid4().hex[:8])
        if media.kind != "image":
            raise ValueError("هذه الأداة تعمل على الصور الثابتة فقط. أرسل صورة PNG أو JPG أو WEBP.")
        extension = media.path.suffix.lower() or ".png"
        destination = session_dir / f"source{extension}"
        shutil.copy2(media.path, destination)
        return destination
    except Exception:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)


@router.message(Command("d", "edit"))
async def start_editor(message: Message, bot: Bot, db: Database, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    await db.touch_user(user.id, user.username, user.first_name)
    if await db.is_blocked(user.id) or (await db.get_maintenance() and user.id not in settings.admin_ids):
        await message.answer("البوت غير متاح لحسابك حاليًا.")
        return
    await state.clear()
    try:
        source = await _copy_replied_image(message, bot, user.id, "edit")
    except (ValueError, MediaIntakeError) as exc:
        await message.answer(html.escape(str(exc)))
        return
    except Exception:
        log.exception("Could not initialize editor session for user %s", user.id)
        await message.answer("تعذر فتح الصورة للمحرر. جرّب صورة أصغر.")
        return
    session_id = source.parent.name
    await db.create_edit_session(session_id, user.id, str(source), str(source))
    await state.set_state(FeatureStates.editing)
    await state.update_data(
        editor_original=str(source), editor_current=str(source),
        editor_session_id=session_id, editor_page=0,
        editor_history=[], editor_operations=[],
    )
    await message.answer_document(
        FSInputFile(source, filename="sticker-edit.png"),
        caption="🖌 <b>محرر Sticker Studio</b>\nاختر أداة من الأزرار. يمكنك التراجع إلى الأصل في أي وقت.",
        reply_markup=_editor_keyboard(),
    )


def _editor_tool(action: str, argument: str) -> tuple[str, dict[str, object]]:
    """Map a callback name to (Pillow action, options)."""
    presets: dict[str, tuple[str, dict[str, object]]] = {
        "crop": ("crop", {}),
        "crop_45": ("crop_45", {}),
        "crop_169": ("crop_169", {}),
        "rotate": ("rotate", {}),
        "resize": ("resize", {}),
        "flip_h": ("flip_h", {}),
        "flip_v": ("flip_v", {}),
        "outline": ("outline", {}),
        "shadow": ("shadow", {}),
        "blur": ("blur", {}),
        "sharpen": ("sharpen", {}),
        "grayscale": ("grayscale", {}),
        "sepia": ("sepia", {}),
        "watermark": ("watermark", {}),
        "text_big": ("text", {"size": 52}),
        "brightness_up": ("brightness", {"factor": 1.35}),
        "brightness_down": ("brightness", {"factor": 0.7}),
        "contrast_up": ("contrast", {"factor": 1.4}),
        "saturation_up": ("saturation", {"factor": 1.6}),
    }
    if action == "bg":
        color = (argument or "white").strip().lower()
        if color not in {"white", "black", "blue", "red", "green", "yellow"}:
            raise EditorError("لون الخلفية غير مدعوم.")
        return "background", {"color": color}
    if action in presets:
        return presets[action]
    raise EditorError("أداة التحرير غير معروفة.")


async def _send_editor_preview(
    message: Message, path: Path, page: int, note: str
) -> None:
    await message.answer_document(
        FSInputFile(path, filename="sticker-edit.png"),
        caption=(
            f"{note}\n"
            "🖼 المعاينة PNG 512×512 — يمكنك متابعة التعديل، أو ↩️ التراجع، "
            "أو ✅ إنهاء لتحويلها إلى WEBP وإضافتها للحزمة."
        ),
        reply_markup=_editor_keyboard(page),
    )


def _template_keyboard(rows: list[dict[str, object]]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=f"▶️ {str(row['title'])[:40]}", callback_data=f"editor:tpl:{row['id']}"
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"editor:tpl_del:{row['id']}"),
        ]
        for row in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _template_action(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
    data: dict,
    action: str,
    argument: str,
    operations: list[dict],
) -> None:
    user_id = callback.from_user.id
    if action == "save_tpl":
        if not operations:
            await callback.answer(
                "لا توجد خطوات بعد. طبّق أداة واحدة على الأقل ثم احفظ القالب.",
                show_alert=True,
            )
            return
        await state.set_state(FeatureStates.waiting_template_title)
        await state.update_data(editor_operations=operations)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"💾 حفظ قالب من {len(operations)} خطوة.\n"
                "أرسل اسم القالب (حتى 64 حرفًا). /cancel للإلغاء."
            )
        return
    if action == "tplist":
        rows = await db.get_templates(user_id)
        await callback.answer()
        if not rows:
            if callback.message:
                await callback.message.answer(
                    "📂 لا توجد قوالب محفوظة. عدّل صورة ثم اضغط «💾 حفظ كقالب»."
                )
            return
        if callback.message:
            await callback.message.answer(
                "📂 <b>قوالبي</b>\n"
                "▶️ يطبّق القالب على الصورة الحالية (من الأصل)، 🗑 يحذفه:",
                reply_markup=_template_keyboard(rows),
            )
        return
    if action == "tpl_del":
        try:
            template_id = int(argument)
        except ValueError:
            await callback.answer("طلب غير صالح.", show_alert=True)
            return
        removed = await db.delete_template(template_id, user_id)
        await callback.answer("🗑 حُذف القالب." if removed else "القالب غير موجود.", show_alert=not removed)
        return
    if action != "tpl":
        await callback.answer("إجراء غير معروف.", show_alert=True)
        return
    try:
        template_id = int(argument)
    except ValueError:
        await callback.answer("طلب غير صالح.", show_alert=True)
        return
    original = _safe_session_path(user_id, data.get("editor_original"))
    if not original:
        await callback.answer("ابدأ جلسة /edit أولاً ثم طبّق القالب.", show_alert=True)
        return
    try:
        title, saved = await templates.apply_saved(db, user_id, template_id)
    except TemplateError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    output = original.parent / f"tpl_{uuid.uuid4().hex[:10]}.png"
    await callback.answer("جارٍ تطبيق القالب…")
    try:
        await run_tracked(
            db, user_id, "template_apply", templates.apply_template,
            original, output, saved,
        )
    except TemplateError as exc:
        if callback.message:
            await callback.message.answer(str(exc))
        return
    except Exception:
        log.exception("Template %s failed for %s", template_id, user_id)
        if callback.message:
            await callback.message.answer("تعذر تطبيق القالب.")
        return
    await db.record_edit(user_id, "template", f"قالب: {title}")
    await state.set_state(FeatureStates.editing)
    await state.update_data(
        editor_current=str(output),
        editor_history=[str(original)],
        editor_operations=saved,
    )
    if data.get("editor_session_id"):
        await db.update_edit_session(str(data["editor_session_id"]), str(output), "active")
    if callback.message:
        await _send_editor_preview(
            callback.message, output, int(data.get("editor_page") or 0),
            f"📂 طُبّق القالب «{title}» من الأصل.",
        )


@router.message(FeatureStates.waiting_template_title, F.text)
async def template_title(message: Message, state: FSMContext, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    value = (message.text or "").strip()
    if value.startswith("/"):
        await state.set_state(FeatureStates.editing)
        await message.answer(
            "تم إلغاء حفظ القالب. جلسة التحرير ما زالت مفتوحة.",
            reply_markup=_editor_keyboard(int((await state.get_data()).get("editor_page") or 0)),
        )
        return
    data = await state.get_data()
    operations = [item for item in (data.get("editor_operations") or [])
                  if isinstance(item, dict)]
    try:
        await templates.save_template(db, user.id, value, operations)
    except TemplateError as exc:
        await message.answer(str(exc))
        return
    await state.set_state(FeatureStates.editing)
    await message.answer(
        f"💾 حُفظ القالب «{value}». يظهر الآن في «📂 قوالبي».",
        reply_markup=_editor_keyboard(int(data.get("editor_page") or 0)),
    )


@router.message(Command("t", "templates"))
async def templates_command(message: Message, state: FSMContext, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    rows = await db.get_templates(user.id)
    if not rows:
        await message.answer(
            "📂 لا توجد قوالب محفوظة.\n"
            "استخدم /edit على صورة، طبّق أدواتك، ثم اضغط «💾 حفظ كقالب»."
        )
        return
    await message.answer(
        "📂 <b>قوالبي</b>\nيُطبّق القالب داخل جلسة /edit الحالية:",
        reply_markup=_template_keyboard(rows),
    )


@router.callback_query(F.data.startswith("editor:"))
async def editor_action(callback: CallbackQuery, bot: Bot, db: Database, state: FSMContext) -> None:
    user_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    argument = parts[2] if len(parts) > 2 else ""
    current_state = await state.get_state()
    if current_state not in (
        FeatureStates.editing.state,
        FeatureStates.waiting_editor_text.state,
        FeatureStates.waiting_template_title.state,
    ):
        await callback.answer("جلسة التحرير انتهت. استخدم /edit من جديد.", show_alert=True)
        return
    data = await state.get_data()
    operations = [item for item in (data.get("editor_operations") or [])
                  if isinstance(item, dict)]

    if action in {"save_tpl", "tplist"} or action.startswith("tpl"):
        await _template_action(callback, db, state, data, action, argument, operations)
        return

    if action == "page":
        try:
            page = int(argument)
        except ValueError:
            page = 0
        page %= len(EDITOR_TOOL_PAGES)
        await state.set_state(FeatureStates.editing)
        await state.update_data(editor_page=page)
        await callback.answer(f"صفحة الأدوات {page + 1}")
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=_editor_keyboard(page))
            except Exception:
                log.debug("could not swap editor keyboard page", exc_info=True)
        return

    if action == "finish":
        current = _safe_session_path(user_id, data.get("editor_current"))
        if current and callback.message:
            final_path = current.parent / f"sticker_{uuid.uuid4().hex[:8]}.webp"
            try:
                from .converter import convert_image_to_webp
                await run_tracked(
                    db, user_id, "editor_finalize", convert_image_to_webp, current, final_path, False,
                )
                await callback.message.answer_document(
                    FSInputFile(final_path, filename="sticker.webp"),
                    caption="✅ ملصق WEBP جاهز لتيليجرام.",
                )
                me = await bot.get_me()
                if me.username:
                    auto_on = await auto_manager.enabled(db, user_id)
                    outcomes = await auto_manager.handle_outputs(
                        bot, db, user_id,
                        [OutputFile(final_path, "WEBP", "")],
                        bot_username=me.username,
                        first_name=callback.from_user.first_name,
                        source_kind="editor",
                    )
                    summary = auto_manager.summary_text(
                        outcomes, converted=1, auto_enabled=auto_on
                    )
                    if summary:
                        await callback.message.answer(
                            summary,
                            reply_markup=auto_manager.summary_keyboard(outcomes),
                        )
            except Exception:
                log.exception("Could not finalize edited sticker for %s", user_id)
                await callback.message.answer("تعذر تجهيز WEBP النهائي. يمكنك إرسال صورة المعاينة للبوت لتحويلها.")
        original = _safe_session_path(user_id, data.get("editor_original"))
        if data.get("editor_session_id"):
            await db.update_edit_session(str(data["editor_session_id"]), str(current or ""), "completed")
        if original:
            shutil.rmtree(original.parent, ignore_errors=True)
        await state.clear()
        await callback.answer("تم إنهاء التحرير.")
        return
    if action in {"text", "text_big"}:
        await state.set_state(FeatureStates.waiting_editor_text)
        await state.update_data(editor_text_options={"size": 52} if action == "text_big" else {})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل النص الذي تريد إضافته (حتى 80 حرفًا، و3 أسطر). /cancel للإلغاء."
            )
        return
    original = _safe_session_path(user_id, data.get("editor_original"))
    current = _safe_session_path(user_id, data.get("editor_current"))
    history = [item for item in (data.get("editor_history") or []) if isinstance(item, str)]
    page = int(data.get("editor_page") or 0)
    if not original or not current:
        await callback.answer("ملفات جلسة التحرير غير موجودة. ابدأ /edit من جديد.", show_alert=True)
        await state.clear()
        return
    if action == "ai_analyze":
        try:
            report = await asyncio.to_thread(describe_file, current)
        except (LightAIError, OSError):
            await callback.answer("تعذر تحليل الصورة.", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(report, reply_markup=_editor_keyboard(page))
        return
    if action in {"ai_auto", "ai_enhance", "ai_upscale"}:
        tool = {"ai_auto": "auto", "ai_enhance": "enhance", "ai_upscale": "upscale"}[action]
        output = current.parent / f"ai_{uuid.uuid4().hex[:10]}.png"
        try:
            await run_tracked(
                db, user_id, f"light_ai_{tool}", run_light_tool, current, output, tool,
            )
        except LightAIError as exc:
            await callback.answer(str(exc)[:190], show_alert=True)
            return
        except Exception:
            log.exception("Light AI %s failed for %s", tool, user_id)
            await callback.answer("تعذر تنفيذ أداة الذكاء الخفيف.", show_alert=True)
            return
        history.append(str(current))
        history = history[-12:]
        await db.record_edit(user_id, f"ai_{tool}", original.name)
        await state.update_data(editor_current=str(output), editor_history=history)
        if data.get("editor_session_id"):
            await db.update_edit_session(str(data["editor_session_id"]), str(output), "active")
        await callback.answer("تم تطبيق أداة الذكاء الخفيف.")
        if callback.message:
            await _send_editor_preview(
                callback.message, output, page, "✅ نتيجة الذكاء الخفيف جاهزة."
            )
        return
    if action == "undo":
        if history:
            candidate = _safe_session_path(user_id, history[-1])
            history = history[:-1]
            result = candidate or original
            note = "↩️ رجعنا خطوة للخلف."
            if operations:
                operations = operations[:-1]
        else:
            result = original
            note = "🔙 لا يوجد تعديل سابق؛ رجعنا للأصل."
            operations = []
        await state.update_data(
            editor_current=str(result), editor_history=history,
            editor_operations=operations,
        )
        if data.get("editor_session_id"):
            await db.update_edit_session(str(data["editor_session_id"]), str(result), "active")
        await callback.answer("تم التراجع.")
        if callback.message:
            await _send_editor_preview(callback.message, result, page, note)
        return
    if action == "reset":
        await state.update_data(
            editor_current=str(original), editor_history=[], editor_operations=[]
        )
        if data.get("editor_session_id"):
            await db.update_edit_session(str(data["editor_session_id"]), str(original), "active")
        await callback.answer("رجعنا للأصل.")
        if callback.message:
            await _send_editor_preview(callback.message, original, page, "🔙 رجعنا للأصل تمامًا.")
        return
    output = current.parent / f"edit_{uuid.uuid4().hex[:10]}.png"
    operation_entry: dict[str, object] | None = None
    try:
        if action in {"removebg", "background"}:
            if not await ai_manager.background_enabled(db):
                raise AIUnavailable("إزالة الخلفية معطلة من إعدادات الاستضافة.")
            await run_tracked(db, user_id, "background_remove", remove_background, current, output)
            operation = "background_remove"
        else:
            tool, options = _editor_tool(action, argument)
            await run_tracked(db, user_id, f"image_{tool}", edit_image, current, output, tool, None, options)
            operation = tool
            operation_entry = {"action": tool, "options": options}
    except (EditorError, AIUnavailable) as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    except Exception:
        log.exception("Editor operation %s failed for %s", action, user_id)
        await callback.answer("تعذر تنفيذ التأثير.", show_alert=True)
        return
    history.append(str(current))
    history = history[-12:]
    if operation_entry is not None:
        operations.append(operation_entry)
        operations = operations[-12:]
    await db.record_edit(user_id, operation, original.name)
    await state.update_data(
        editor_current=str(output), editor_history=history,
        editor_operations=operations,
    )
    if data.get("editor_session_id"):
        await db.update_edit_session(str(data["editor_session_id"]), str(output), "active")
    await callback.answer(f"تم تطبيق {operation}.")
    if callback.message:
        await _send_editor_preview(callback.message, output, page, "✅ تم تطبيق التعديل.")


@router.message(FeatureStates.waiting_editor_text, F.text)
async def editor_text(message: Message, state: FSMContext, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    value = (message.text or "").strip()
    if value.startswith("/"):
        await state.set_state(FeatureStates.editing)
        if value.split()[0] == "/cancel":
            await message.answer("تم إلغاء إضافة النص. جلسة التحرير ما زالت مفتوحة.", reply_markup=_editor_keyboard())
        else:
            await message.answer("أرسل نصًا عاديًا، أو /cancel لإلغاء إضافة النص.")
        return
    data = await state.get_data()
    current = _safe_session_path(user.id, data.get("editor_current"))
    original = _safe_session_path(user.id, data.get("editor_original"))
    if not current or not original:
        await state.clear()
        await message.answer("انتهت جلسة التحرير. استخدم /edit من جديد.")
        return
    options = data.get("editor_text_options") or {}
    output = current.parent / f"text_{uuid.uuid4().hex[:10]}.png"
    try:
        await run_tracked(
            db, user.id, "image_text", edit_image, current, output, "text", value, options,
        )
    except EditorError as exc:
        await message.answer(str(exc))
        return
    history = [item for item in (data.get("editor_history") or []) if isinstance(item, str)]
    history.append(str(current))
    history = history[-12:]
    operations = [item for item in (data.get("editor_operations") or [])
                  if isinstance(item, dict)]
    entry: dict[str, object] = {"action": "text", "text": value}
    if options:
        entry["options"] = options
    operations.append(entry)
    operations = operations[-12:]
    await db.record_edit(user.id, "text", original.name)
    await state.update_data(
        editor_current=str(output), editor_history=history,
        editor_operations=operations,
    )
    if data.get("editor_session_id"):
        await db.update_edit_session(str(data["editor_session_id"]), str(output), "active")
    await state.set_state(FeatureStates.editing)
    await _send_editor_preview(
        message, output, int(data.get("editor_page") or 0), "✅ تمت إضافة النص."
    )


@router.message(Command("a", "animate"))
async def start_animation(message: Message, bot: Bot, db: Database, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    await db.touch_user(user.id, user.username, user.first_name)
    if await db.is_blocked(user.id) or (await db.get_maintenance() and user.id not in settings.admin_ids):
        await message.answer("البوت غير متاح لحسابك حاليًا.")
        return
    await state.clear()
    try:
        source = await _copy_replied_image(message, bot, user.id, "animate")
    except (ValueError, MediaIntakeError) as exc:
        await message.answer(html.escape(str(exc)))
        return
    except Exception:
        log.exception("Could not initialize animation session for user %s", user.id)
        await message.answer("تعذر فتح الصورة للتحريك.")
        return
    session_id = source.parent.name
    await db.create_edit_session(session_id, user.id, str(source), str(source))
    await state.set_state(FeatureStates.animating)
    await state.update_data(
        animation_source=str(source), animation_session_id=session_id,
        animation_duration=3.0, animation_speed=1.0,
    )
    await message.answer(
        "🎞 <b>استوديو التحريك</b>\n"
        "اختر الحركة، ثم المدة والسرعة. كل تأثير ينتج ملصق فيديو WEBM (VP9 + شفافية) "
        "بحد أقصى 3 ثوانٍ و256KB — وهو حد تيليجرام.",
        reply_markup=_animation_keyboard(3.0, 1.0),
    )


@router.callback_query(F.data.startswith("animate:"))
async def animation_action(callback: CallbackQuery, bot: Bot, db: Database, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    argument = parts[2] if len(parts) > 2 else ""
    user_id = callback.from_user.id
    data = await state.get_data()
    source = _safe_session_path(user_id, data.get("animation_source"))
    try:
        duration = float(data.get("animation_duration") or 3.0)
        speed = float(data.get("animation_speed") or 1.0)
    except (TypeError, ValueError):
        duration, speed = 3.0, 1.0
    if action == "finish":
        if data.get("animation_session_id"):
            await db.update_edit_session(str(data["animation_session_id"]), str(source or ""), "completed")
        if source:
            shutil.rmtree(source.parent, ignore_errors=True)
        await state.clear()
        await callback.answer("تم إغلاق استوديو التحريك.")
        return
    if action in {"set", "speed"}:
        try:
            value = float(argument)
        except ValueError:
            await callback.answer("قيمة غير صالحة.", show_alert=True)
            return
        try:
            if action == "set":
                duration = clamp_duration(value)
                await state.update_data(animation_duration=duration)
            else:
                speed = clamp_speed(value)
                await state.update_data(animation_speed=speed)
        except AnimationError as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
            return
        if callback.message:
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=_animation_keyboard(duration, speed)
                )
            except Exception:
                log.debug("could not refresh animation keyboard", exc_info=True)
        await callback.answer(f"المدة {duration:g}s — السرعة {speed:g}×")
        return
    if await state.get_state() != FeatureStates.animating.state or not source:
        await callback.answer("جلسة التحريك انتهت. استخدم /animate من جديد.", show_alert=True)
        await state.clear()
        return
    output = source.parent / f"{action}_{uuid.uuid4().hex[:8]}.webm"
    await callback.answer("جارٍ إنشاء الحركة…")
    try:
        info = await run_tracked(
            db, user_id, "animation", render_effect, source, output, action,
            settings.ffmpeg_path, settings.max_processing_seconds, settings.ffprobe_path,
            duration, speed,
        )
        if callback.message:
            preview = output.with_name(output.stem + "_preview.png")
            try:
                await asyncio.to_thread(
                    extract_frame, output, preview, info["duration"] / 2, settings.ffmpeg_path,
                )
                await callback.message.answer_photo(
                    FSInputFile(preview),
                    caption=f"👁 معاينة إطار من الحركة ({info['duration']:.1f}s).",
                )
            except AnimationError:
                log.debug("could not render animation preview frame", exc_info=True)
            label = EFFECT_LABELS.get(info["effect"], info["effect"])
            await callback.message.answer_document(
                FSInputFile(output, filename="animated-sticker.webm"),
                caption=(
                    f"✅ {label} — WEBM / VP9 — {info['duration']:.1f}s — "
                    f"{info['size'] // 1024} KB — 512×512"
                ),
                reply_markup=_animation_keyboard(duration, speed),
            )
        await db.record_edit(user_id, f"animate_{info['effect']}", source.name)
        if callback.message:
            me = await bot.get_me()
            if me.username:
                auto_on = await auto_manager.enabled(db, user_id)
                outcomes = await auto_manager.handle_outputs(
                    bot, db, user_id,
                    [OutputFile(output, "WEBM", "")],
                    bot_username=me.username,
                    first_name=callback.from_user.first_name,
                    source_kind="animation",
                )
                summary = auto_manager.summary_text(
                    outcomes, converted=1, auto_enabled=auto_on
                )
                if summary:
                    await callback.message.answer(
                        summary, reply_markup=auto_manager.summary_keyboard(outcomes)
                    )
    except AnimationError as exc:
        if callback.message:
            await callback.message.answer(str(exc))
    except Exception:
        log.exception("Animation generation failed for user %s", user_id)
        if callback.message:
            await callback.message.answer("تعذر إنشاء الملصق المتحرك. راجع دعم FFmpeg لـ libvpx-vp9.")


@router.message(Command("s", "settings"))
async def user_settings(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.touch_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    prefs = await db.get_user_preferences(message.from_user.id)
    await message.answer("⚙️ <b>إعداداتك</b>\nغيّر الخيارات من الأزرار التالية:", reply_markup=_preferences_keyboard(prefs))


@router.callback_query(F.data.startswith("prefs:"))
async def preferences_action(callback: CallbackQuery, db: Database) -> None:
    user_id = callback.from_user.id
    action = (callback.data or "").split(":", 1)[1]
    prefs = await db.get_user_preferences(user_id)
    if action == "bg":
        if not settings.enable_bg_removal:
            await callback.answer("إزالة الخلفية معطلة من إعدادات الاستضافة.", show_alert=True)
            return
        await db.set_user_preference(user_id, "bg_removal", not bool(prefs["bg_removal"]))
    elif action == "auto":
        if not settings.auto_pack:
            await callback.answer("إنشاء الحزم التلقائي معطل من إعدادات الاستضافة.", show_alert=True)
            return
        await db.set_user_preference(user_id, "auto_pack", not bool(prefs["auto_pack"]))
    prefs = await db.get_user_preferences(user_id)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=_preferences_keyboard(prefs))
        except Exception:
            pass
    await callback.answer("تم حفظ إعداداتك.")


@router.message(Command("favorite"))
async def favorite_reply(message: Message, db: Database) -> None:
    user = message.from_user
    target = message.reply_to_message
    if user is None or target is None:
        await message.answer("استخدم /favorite بالرد على صورة أو ملصق أو ملف أرسله البوت.")
        return
    media_type = ""
    file_id = file_unique_id = display_name = ""
    if target.sticker:
        media_type, file_id, file_unique_id = "sticker", target.sticker.file_id, target.sticker.file_unique_id
        display_name = "sticker"
    elif target.document:
        media_type, file_id, file_unique_id = "document", target.document.file_id, target.document.file_unique_id
        display_name = target.document.file_name or "document"
    elif target.photo:
        media_type, file_id, file_unique_id = "photo", target.photo[-1].file_id, target.photo[-1].file_unique_id
        display_name = "photo"
    elif target.animation:
        media_type, file_id, file_unique_id = "animation", target.animation.file_id, target.animation.file_unique_id
        display_name = target.animation.file_name or "animation"
    elif target.video:
        media_type, file_id, file_unique_id = "video", target.video.file_id, target.video.file_unique_id
        display_name = target.video.file_name or "video"
    else:
        await message.answer("نوع الرسالة غير مدعوم في المفضلة.")
        return
    saved = await db.add_favorite(user.id, file_id, file_unique_id, media_type, display_name)
    await message.answer("⭐ تمت الإضافة إلى المفضلة." if saved else "هذا الملف محفوظ بالفعل في المفضلة.")


@router.message(Command("v", "favorites"))
async def list_favorites(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    favorites = await db.get_favorites(message.from_user.id)
    if not favorites:
        await message.answer("لا توجد عناصر في المفضلة. رد على ملف باستخدام /favorite لحفظه.")
        return
    rows = []
    for item in favorites:
        rows.append([
            InlineKeyboardButton(text=f"⭐ {str(item['display_name'])[:34]}",
                                 callback_data=f"favorite:send:{item['id']}"),
            InlineKeyboardButton(text="حذف", callback_data=f"favorite:delete:{item['id']}"),
        ])
    await message.answer("⭐ <b>مفضلتك</b> — اختر عنصرًا لإرساله مرة أخرى:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("favorite:"))
async def send_favorite(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else "send"
    try:
        favorite_id = int(parts[2] if len(parts) > 2 else parts[1])
    except (ValueError, IndexError):
        await callback.answer("عنصر غير صالح.", show_alert=True)
        return
    if action == "delete":
        removed = await db.delete_favorite(favorite_id, callback.from_user.id)
        await callback.answer("تم الحذف." if removed else "العنصر غير موجود.")
        if removed and callback.message:
            await callback.message.edit_text("تم حذف العنصر من المفضلة. استخدم /favorites للتحديث.")
        return
    item = await db.get_favorite(favorite_id, callback.from_user.id)
    if not item:
        await callback.answer("لم أجد العنصر.", show_alert=True)
        return
    method = {
        "sticker": bot.send_sticker,
        "document": bot.send_document,
        "photo": bot.send_photo,
        "animation": bot.send_animation,
        "video": bot.send_video,
    }.get(str(item["media_type"]))
    if method is None:
        await callback.answer("نوع الملف غير مدعوم.", show_alert=True)
        return
    try:
        await method(chat_id=callback.from_user.id, **{
            "sticker" if item["media_type"] == "sticker" else
            "document" if item["media_type"] == "document" else
            "photo" if item["media_type"] == "photo" else
            "animation" if item["media_type"] == "animation" else "video": item["file_id"]
        })
        await callback.answer("تم إرسال العنصر.")
    except Exception:
        await callback.answer("تعذر إرسال العنصر؛ ربما لم يعد متاحًا في تيليجرام.", show_alert=True)


@router.message(Command("k", "tasks"))
async def task_history(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    rows = await db.get_recent_tasks(message.from_user.id, 10)
    if not rows:
        await message.answer("لا توجد مهام مسجلة بعد.")
        return
    labels = {"queued": "بالانتظار", "running": "قيد التنفيذ", "completed": "مكتملة", "failed": "فشلت", "cancelled": "ملغاة"}
    lines = ["<b>آخر مهامك</b>"]
    for row in rows:
        lines.append(f"• {html.escape(str(row['task_type']))}: {labels.get(str(row['status']), row['status'])} — {row['progress']}%")
    await message.answer("\n".join(lines))


@router.message(Command("profile"))
async def profile(message: Message, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    await db.touch_user(user.id, user.username, user.first_name)
    prefs = await db.get_user_preferences(user.id)
    favorites = await db.get_favorites(user.id, 50)
    tasks = await db.get_recent_tasks(user.id, 20)
    complete = sum(1 for item in tasks if item["status"] == "completed")
    await message.answer(
        "👤 <b>ملفك في Sticker Studio</b>\n"
        f"ID: <code>{user.id}</code>\n"
        f"إزالة الخلفية: {'مفعلة' if prefs['bg_removal'] else 'متوقفة'}\n"
        f"المفضلة: {len(favorites)}\nالمهام المكتملة ضمن آخر 20: {complete}"
    )


@router.message(Command("packimport"))
async def pack_import_command(message: Message, bot: Bot, db: Database, bot_username: str) -> None:
    target = message.reply_to_message
    if target is None:
        await message.answer("رد على ملف ZIP الخاص بحزمة الملصقات باستخدام /packimport. ويمكنك أيضًا إرسال ZIP مباشرة للبوت ليتم فحصه وتحويل محتوياته المدعومة.")
        return
    if not message.from_user:
        return
    # Reuse the verified archive intake/conversion pipeline. Existing ZIP
    # extraction safeguards, duplicate detection and pack-format checks remain active.
    from .handlers import _check_access, _process_messages
    if not await _check_access(message, db, cooldown=False):
        return
    await message.answer("⏳ بدأت استيراد الحزمة. سأفحص محتويات ZIP وأضيف الملفات المدعومة.")
    await _process_messages([target], bot, db, bot_username)


@router.message(Command("edithistory"))
async def edit_history(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    rows = await db.get_edit_history(message.from_user.id, 15)
    if not rows:
        await message.answer("لا يوجد سجل تعديلات بعد. استخدم /edit لتحرير صورة.")
        return
    lines = ["<b>سجل التعديلات الأخير</b>"]
    for row in rows:
        lines.append(f"• {html.escape(str(row['operation']))} — {html.escape(str(row['source_name']))}")
    await message.answer("\n".join(lines))


@router.message(Command("packexport"))
async def pack_export_command(message: Message, bot: Bot, db: Database) -> None:
    if not message.from_user:
        return
    active = await db.get_active_pack(message.from_user.id)
    if not active.name:
        await message.answer("لا توجد حزمة نشطة للتصدير. استخدم /packs لاختيار حزمة.")
        return
    work = Path(tempfile.mkdtemp(prefix="sticker-pack-export-", dir=settings.work_dir))
    archive = work / "sticker-pack-export.zip"
    try:
        manifest = await export_pack(bot, active.name, archive)
        await message.answer_document(
            FSInputFile(archive, filename=f"{active.name}.zip"),
            caption=f"✅ تم تصدير {manifest['sticker_count']} ملصقًا من {html.escape(str(manifest['title']))}."
        )
    except PackExportError as exc:
        await message.answer(str(exc))
    except Exception:
        log.exception("Pack export failed for %s", message.from_user.id)
        await message.answer("تعذر تصدير الحزمة. حاول مرة أخرى.")
    finally:
        shutil.rmtree(work, ignore_errors=True)
