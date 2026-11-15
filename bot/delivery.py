"""Result delivery with retries and safe status messages (aiogram)."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile, Message, ReplyParameters

from .converter import ConversionError, OutputFile

log = logging.getLogger("sticker-studio.delivery")


class DeliveryError(Exception):
    """The result converted successfully but Telegram would not accept it."""


def new_trace_id() -> str:
    """Short, non-secret id that links a user report to one log entry."""
    return secrets.token_hex(4)


def _delivery_reason(error: Exception | None) -> str:
    if error is None:
        return "خطأ غير معروف."
    if isinstance(error, TelegramRetryAfter):
        return "تيليجرام طلب الانتظار قبل إرسال ملف آخر (حد المعدل)."
    if isinstance(error, TelegramEntityTooLarge):
        return "حجم الناتج أكبر من الحد الذي يسمح به تيليجرام."
    if isinstance(error, TelegramNetworkError):
        return "تعذر الاتصال بتيليجرام أثناء رفع الملف."
    if isinstance(error, TelegramForbiddenError):
        return "البوت لا يملك صلاحية إرسال الملفات في هذه المحادثة."
    if isinstance(error, TelegramBadRequest):
        text = str(error).lower()
        if "reply" in text or "replied" in text:
            return "تعذر الرد على الرسالة الأصلية، ورفض تيليجرام الإرسال المستقل."
        return "رفض تيليجرام الملف."
    return f"خطأ غير متوقع أثناء الإرسال ({type(error).__name__})."


async def send_document(
    bot: Bot,
    chat_id: int,
    path: Path,
    caption: str,
    trace_id: str,
    reply_to_message_id: int | None = None,
) -> None:
    """Send exactly one result as a Document, retrying transient failures."""
    if not path.exists():
        raise DeliveryError("تعذر العثور على الملف الناتج بعد التحويل. أعد إرسال الملف.")
    last_error: Exception | None = None
    use_reply = reply_to_message_id is not None
    # Two attempts bound the worst case at roughly two minutes instead of
    # freezing the queue for six when the network throttles uploads.
    attempts = 2
    started = time.monotonic()
    log.info(
        "[%s] uploading %s (%d KB) to chat %s",
        trace_id, path.name, path.stat().st_size // 1024, chat_id,
    )

    for attempt in range(1, attempts + 1):
        try:
            document = FSInputFile(path, filename=path.name)
            kwargs = {
                "chat_id": chat_id,
                "document": document,
                "caption": caption[:1024],
            }
            if use_reply:
                kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to_message_id)
            await bot.send_document(**kwargs)
            log.info(
                "[%s] delivered %s in %.1fs",
                trace_id, path.name, time.monotonic() - started,
            )
            return
        except TelegramRetryAfter as exc:
            last_error = exc
            wait = float(exc.retry_after) + 1.0
            log.warning(
                "[%s] send document attempt %d/%d rate-limited; waiting %.1fs",
                trace_id, attempt, attempts, wait,
            )
            await asyncio.sleep(wait)
        except TelegramBadRequest as exc:
            last_error = exc
            text = str(exc).lower()
            if use_reply and ("reply" in text or "replied" in text):
                use_reply = False
                log.warning(
                    "[%s] reply target unavailable; retrying as a direct chat upload",
                    trace_id,
                )
                continue
            log.warning(
                "[%s] send document attempt %d/%d rejected: %s",
                trace_id, attempt, attempts, exc,
            )
            break
        except TelegramForbiddenError as exc:
            last_error = exc
            log.warning(
                "[%s] send document attempt %d/%d forbidden: %s",
                trace_id, attempt, attempts, exc,
            )
            break
        except Exception as exc:
            last_error = exc
            log.warning(
                "[%s] send document attempt %d/%d failed: %s: %s",
                trace_id, attempt, attempts, type(exc).__name__, exc,
                exc_info=True,
            )
            if attempt < attempts:
                await asyncio.sleep(2 ** attempt)

    log.warning(
        "[%s] delivery gave up after %.1fs: %s",
        trace_id, time.monotonic() - started, _delivery_reason(last_error),
    )
    raise DeliveryError(
        "تم تجهيز الملف لكن تعذر إرساله إلى تيليجرام.\n"
        f"السبب: {_delivery_reason(last_error)}\n"
        "أعد المحاولة بعد قليل."
    ) from last_error


async def send_results(
    bot: Bot,
    chat_id: int,
    outputs: Sequence[OutputFile],
    work_dir: Path,
    trace_id: str,
    reply_to_message_id: int | None = None,
) -> None:
    if not outputs:
        raise ConversionError("لم ينتج أي ملف.")

    # One result is sent directly; multiple results are packed into one ZIP.
    if len(outputs) == 1:
        item = outputs[0]
        caption = f"✅ جهزت الملف لك\n{item.note}\n\nيمكنك رفعه الآن إلى @Stickers."
        await send_document(
            bot, chat_id, item.path, caption, trace_id, reply_to_message_id
        )
        return

    archive_path = work_dir / "StickerStudio_Results.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in outputs:
            archive.write(item.path, arcname=item.path.name)
    caption_lines = [f"✅ جهزت {len(outputs)} ملفًا بنجاح."]
    caption_lines.extend(f"• {item.path.name}: {item.kind}" for item in outputs[:12])
    if len(outputs) > 12:
        caption_lines.append(f"• و{len(outputs) - 12} ملفًا آخر")
    caption_lines.append("\nفك ضغط الملف وارفع النتائج إلى @Stickers.")
    await send_document(
        bot, chat_id, archive_path, "\n".join(caption_lines), trace_id, reply_to_message_id
    )


async def edit_status(message: Message | None, text: str) -> bool:
    if message is None:
        return False
    try:
        await message.edit_text(text)
        return True
    except Exception:
        log.debug("could not edit status message", exc_info=True)
        return False


async def delete_status(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        log.debug("could not delete status message", exc_info=True)


async def notify(bot: Bot, message: Message, status: Message | None, text: str) -> None:
    """Tell the user what went wrong, even if the status message is gone."""
    if status is not None and await edit_status(status, text):
        return
    try:
        await bot.send_message(message.chat.id, text)
    except Exception:
        log.warning("could not notify the user", exc_info=True)
