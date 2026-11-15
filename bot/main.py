"""Entry point for the merged Sticker Studio bot (aiogram, long polling)."""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent, Message

from .config import settings
from .database import Database
from .backups import backup_loop
from .handlers import router
from .feature_handlers import router as feature_router
from .packs.manager_handlers import router as pack_manager_router
from .tasks.maintenance import purge_feature_sessions, purge_work_dir
from .tasks.runtime import start_queue, stop_queue

VERSION = "4.1.1"
TOKEN_PATTERN = re.compile(r"bot\d{8,}:[A-Za-z0-9_-]{20,}")
log = logging.getLogger("sticker-studio")


class RedactingFormatter(logging.Formatter):
    """Ensure a bot token can never reach a log line, even via a traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return TOKEN_PATTERN.sub("bot<redacted>", super().format(record))


def configure_logging() -> None:
    # Windows consoles default to a legacy code page and mangle Arabic lines.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        handlers=[handler],
        force=True,
    )
    for noisy in ("httpx", "httpcore", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def prefer_ipv4() -> None:
    """Put IPv4 first when Telegram is reachable over both families.

    Some hosting networks announce IPv6 but stall on it, surfacing as a
    ReadTimeout during Telegram's bootstrap. Set FORCE_IPV4=0 to opt out.
    """
    if not settings.force_ipv4:
        return
    original = socket.getaddrinfo

    def ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
        infos = original(host, port, family, type, proto, flags)
        return sorted(infos, key=lambda item: 0 if item[0] == socket.AF_INET else 1)

    socket.getaddrinfo = ipv4_first  # type: ignore[assignment]


async def wait_for_telegram(bot: Bot) -> None:
    """Retry the initial getMe handshake forever on transient network errors.

    A short outage during deployment must not crash-loop a host that restarts
    failed processes. Bad tokens still fail immediately with a clear message.
    """
    delay = 3
    while True:
        try:
            # A short handshake timeout keeps retries quick on flaky networks.
            await bot.get_me(request_timeout=25)
            return
        except TelegramUnauthorizedError:
            log.error("BOT_TOKEN was rejected by Telegram. Check the token in .env.")
            raise
        except TelegramConflictError:
            log.error(
                "Another process is already polling this bot token. "
                "Stop the duplicate instance and start again."
            )
            raise
        except TelegramRetryAfter as exc:
            wait = max(delay, float(exc.retry_after) + 1.0)
            log.warning("Telegram asked to wait %.0fs before the startup check", wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, 60)
        except (TelegramNetworkError, TelegramServerError) as exc:
            # 5xx responses (Bad Gateway, etc.) are just as transient as a
            # dropped connection and must not crash-loop the host.
            log.warning(
                "Telegram is unreachable at startup (%s); retrying in %ss", exc, delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        except Exception as exc:
            # Transport libraries can raise their own errors (proxy failures,
            # DNS errors, resets). None of them justify crash-looping the host.
            log.warning(
                "Startup check failed (%s: %s); retrying in %ss",
                type(exc).__name__, exc, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def on_error(event: ErrorEvent) -> None:
    log.error("Unhandled update error", exc_info=event.exception)
    update = event.update
    message = update.message
    if message is None and update.callback_query is not None:
        candidate = update.callback_query.message
        message = candidate if isinstance(candidate, Message) else None
    if message is None:
        return
    try:
        await message.answer(
            "حدث خطأ غير متوقع أثناء معالجة طلبك. أعد المحاولة بعد لحظات."
        )
    except Exception:
        log.debug("could not notify the user about an unhandled error", exc_info=True)


def cleanup_stale_feature_sessions(max_age_hours: int = 24) -> int:
    """Remove abandoned per-user editor/animation workspaces after restarts."""
    return purge_feature_sessions(settings.data_dir, max_age_hours)


async def maintenance_loop(interval_seconds: int = 6 * 3600) -> None:
    """Delete stale temp files forever without ever blocking the bot."""
    while True:
        try:
            work = purge_work_dir(settings.work_dir, 6)
            sessions = purge_feature_sessions(settings.data_dir, 24)
            if work or sessions:
                log.info(
                    "Cleanup removed %d temp entr(ies) and %d stale session(s)",
                    work, sessions,
                )
        except Exception:
            log.warning("Periodic cleanup failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def main() -> None:
    configure_logging()
    prefer_ipv4()
    removed_sessions = cleanup_stale_feature_sessions()
    if removed_sessions:
        log.info("Removed %d abandoned feature session(s)", removed_sessions)
    log.info(
        "Starting Sticker Studio %s (converter + editor + animation + packs + admin) — polling mode",
        VERSION,
    )

    db = Database(settings.database_path)
    await db.init(settings.maintenance_default)

    # A hung Telegram upload must fail quickly instead of freezing the queue.
    proxy = settings.telegram_proxy or None
    session = AiohttpSession(timeout=60, proxy=proxy)
    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )

    cleaner: asyncio.Task | None = None
    backupper: asyncio.Task | None = None
    try:
        await wait_for_telegram(bot)
        me = await bot.get_me(request_timeout=25)
        if not me.username:
            raise RuntimeError("Could not read the bot username from Telegram.")
        username = me.username

        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.workflow_data.update(
            db=db,
            bot_username=username,
            settings=settings,
        )
        # Feature routes come first so their FSM text handlers are not shadowed
        # by the legacy catch-all text handler.
        dispatcher.include_router(pack_manager_router)
        dispatcher.include_router(feature_router)
        dispatcher.include_router(router)
        dispatcher.errors.register(on_error)
        await start_queue()
        cleaner = asyncio.create_task(maintenance_loop(), name="temp-cleanup")
        if settings.backup_enabled:
            backupper = asyncio.create_task(
                backup_loop(
                    settings.database_path,
                    settings.backup_dir,
                    interval_hours=settings.backup_interval_hours,
                    keep=settings.backup_keep,
                ),
                name="database-backup",
            )
            log.info(
                "Periodic database backups enabled every %dh (keep %d) in %s",
                settings.backup_interval_hours,
                settings.backup_keep,
                settings.backup_dir,
            )

        # Polling and webhooks are mutually exclusive on Telegram's side. Keep
        # queued updates so files sent during a restart are not lost silently.
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            log.warning("Could not delete a stale webhook (will still try polling)", exc_info=True)

        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="بدء الاستخدام"),
                    BotCommand(command="help", description="المساعدة"),
                    BotCommand(command="n", description="حزمة جديدة"),
                    BotCommand(command="e", description="إنهاء الحزمة"),
                    BotCommand(command="p", description="حزمي"),
                    BotCommand(command="s", description="إعدادات"),
                    BotCommand(command="h", description="سجل التحويلات"),
                    BotCommand(command="f", description="لقطة من فيديو"),
                    BotCommand(command="m", description="حركة من صورة"),
                    BotCommand(command="a", description="تحريك صورة"),
                    BotCommand(command="d", description="تحرير صورة"),
                    BotCommand(command="t", description="قوالب"),
                    BotCommand(command="v", description="المفضلة"),
                    BotCommand(command="g", description="إدارة الحزم"),
                    BotCommand(command="c", description="إلغاء"),
                ]
            )
        except Exception:
            log.warning("Could not register the Telegram command menu", exc_info=True)

        log.info("Sticker Studio started as @%s", username)
        while True:
            try:
                await dispatcher.start_polling(
                    bot,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                    drop_pending_updates=False,
                    handle_as_tasks=True,
                    close_bot_session=False,
                )
                break
            except TelegramConflictError:
                log.error(
                    "Polling stopped: another bot instance is using this token. "
                    "Keep exactly one running instance."
                )
                raise
            except Exception:
                # A transport-level failure must not take the whole bot down;
                # polling is safe to restart on the same dispatcher.
                log.exception("Polling crashed; restarting in 10s")
                await asyncio.sleep(10)
    finally:
        if cleaner is not None:
            cleaner.cancel()
            try:
                await cleaner
            except (asyncio.CancelledError, Exception):
                pass
        if backupper is not None:
            backupper.cancel()
            try:
                await backupper
            except (asyncio.CancelledError, Exception):
                pass
        await stop_queue()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Sticker Studio stopped.")
