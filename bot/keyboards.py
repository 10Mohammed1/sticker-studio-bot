"""Keyboards for the merged bot."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📷 إرسال صورة أو ملف للتحويل")],
            [
                KeyboardButton(text="📦 حزمتـي الثابتة"),
                KeyboardButton(text="🎞️ حزمتـي المتحركة"),
            ],
            [
                KeyboardButton(text="⚙️ إعدادات الحزم"),
                KeyboardButton(text="📚 سجل التحويلات"),
            ],
            [KeyboardButton(text="📦 إنشاء حزمة"), KeyboardButton(text="📁 حزمتي")],
            [
                KeyboardButton(text="✏️ تعديل الحزمة"),
                KeyboardButton(text="❌ إنهاء الحزمة"),
                KeyboardButton(text="🔄 تحويل تلقائي"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="أرسل صورة أو GIF أو فيديو أو ملفًا مباشرة…",
    )


FORMAT_LABELS = {
    "static": "ثابت WEBP",
    "video": "فيديو WEBM",
    "animated": "متحرك TGS",
}


def packs_keyboard(packs) -> InlineKeyboardMarkup:
    """Inline list of the user's previous sticker packs."""
    rows: list[list[InlineKeyboardButton]] = []
    for pack in packs:
        kind = FORMAT_LABELS.get(pack.sticker_format, pack.sticker_format)
        # Inline button text must stay on one line and inside Telegram's limit.
        title = " ".join(str(pack.title).split()) or "حزمة"
        label = f"{title} — {kind}"
        rows.append(
            [InlineKeyboardButton(text=label[:60], callback_data=f"packs:open:{pack.id}")]
        )
    if rows:
        rows.append([InlineKeyboardButton(text="🔄 تحديث", callback_data="packs:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pack_editor_keyboard(pack) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✏️ تغيير الاسم", callback_data=f"packs:rename:{pack.id}"),
            InlineKeyboardButton(text="➕ إضافة ملصقات", callback_data=f"packs:use:{pack.id}"),
        ]
    ]
    if pack.name:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🛠 إدارة متقدمة (حذف/ترتيب/نسخ/دمج)",
                    callback_data=f"pkm:open:{pack.id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔗 فتح الحزمة في تيليجرام",
                    url=f"https://t.me/addstickers/{pack.name}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ رجوع للحزم", callback_data="packs:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard(maintenance: bool, ai_enabled: bool = False) -> InlineKeyboardMarkup:
    maintenance_text = "🟢 إيقاف وضع الصيانة" if maintenance else "🔴 تفعيل وضع الصيانة"
    ai_text = "🧠 إيقاف إزالة الخلفية" if ai_enabled else "🧠 تفعيل إزالة الخلفية"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin:stats"),
                InlineKeyboardButton(text="👥 المستخدمون", callback_data="admin:users"),
            ],
            [
                InlineKeyboardButton(
                    text="🩺 المراقبة (RAM/القرص/الطابور)", callback_data="admin:monitor"
                )
            ],
            [
                InlineKeyboardButton(text=ai_text, callback_data="admin:ai"),
                InlineKeyboardButton(text="⚠️ سجل الأخطاء", callback_data="admin:errors"),
            ],
            [
                InlineKeyboardButton(text="📥 نسخة احتياطية", callback_data="admin:backup"),
                InlineKeyboardButton(text=maintenance_text, callback_data="admin:maintenance"),
            ],
            [InlineKeyboardButton(text="🔄 تحديث", callback_data="admin:refresh")],
        ]
    )
