"""Telegram "🎵 Bóc thoại TikTok" — paste TikTok/YouTube video links, get their transcribed speech.

Flow: tap the menu button → paste one or more links (no command needed) → the bot transcribes
them in the background and replies. The chat stays in link mode until "✅ Xong" / another menu
button / /cancel. A message made only of video links is also picked up outside link mode.

Callback data: "bocthoai:done"
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from core.link_transcriber import (
    MAX_LINKS_PER_REQUEST,
    build_telegram_messages,
    extract_video_links,
    is_only_links,
    transcribe_links,
)

LOGGER = logging.getLogger("short_video_distributor.thoai_hot")
COMMANDS = ["thoai", "boc_thoai", "thoai_hot"]
CALLBACK_PREFIX = "bocthoai:"
_waiting: set[str] = set()
_job_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task[None]] = set()

PROMPT = (
    "🎵 <b>Bóc thoại video TikTok</b>\n"
    "📎 Dán link video TikTok vào đây (một hoặc nhiều link, tối đa "
    f"{MAX_LINKS_PER_REQUEST} link/tin nhắn). Link YouTube Shorts và Facebook (Reels / video) cũng được.\n"
    "<i>Ví dụ: https://vt.tiktok.com/ZSp4Stubm/</i>\n"
    "Dán xong cứ gửi tiếp link khác; bấm ✅ Xong khi không cần nữa."
)


def is_waiting(chat_id: str | int) -> bool:
    return str(chat_id) in _waiting


def cancel(chat_id: str | int) -> None:
    _waiting.discard(str(chat_id))


def _done_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Xong", callback_data=f"{CALLBACK_PREFIX}done")]]
    )


def _result_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📜 Xem log", callback_data="logview:crawl"),
                InlineKeyboardButton("✅ Xong", callback_data=f"{CALLBACK_PREFIX}done"),
            ]
        ]
    )


async def start_link_mode(message: Any) -> None:
    _waiting.add(str(message.chat_id))
    await message.reply_text(PROMPT, parse_mode=ParseMode.HTML, reply_markup=_done_markup())


async def handle_text(message: Any, text: str) -> bool:
    """Handle a pasted link. Returns True when the message was consumed."""
    chat_id = str(message.chat_id)
    waiting = chat_id in _waiting
    if not waiting and not is_only_links(text):
        return False
    links, ignored = extract_video_links(text)
    if not links:
        if not waiting:
            return False  # e.g. a non-video link pasted while idle: let other handlers decide
        await message.reply_text(
            "⚠️ Không thấy link video TikTok hợp lệ. Link đúng dạng "
            "<code>https://vt.tiktok.com/...</code> hoặc "
            "<code>https://www.tiktok.com/@user/video/123</code>, "
            "<code>https://www.facebook.com/reel/123</code> hoặc <code>https://fb.watch/...</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=_done_markup(),
        )
        return True
    await _start_job(message, links[:MAX_LINKS_PER_REQUEST], ignored, len(links))
    return True


async def _start_job(message: Any, links: list[str], ignored: list[str], total: int) -> None:
    notes = []
    if total > len(links):
        notes.append(f"chỉ lấy {len(links)}/{total} link đầu")
    if ignored:
        notes.append(f"bỏ qua {len(ignored)} link không phải video")
    note = f" ({'; '.join(notes)})" if notes else ""
    queued = " Đang có lượt khác chạy, sẽ chạy ngay sau." if _job_lock.locked() else ""
    await message.reply_text(
        f"⏳ Đang bóc thoại {len(links)} video{note}… (mỗi video khoảng 1–2 phút).{queued}"
    )
    task = asyncio.create_task(_run_and_reply(message, links))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_and_reply(message: Any, links: list[str]) -> None:
    async with _job_lock:
        language = os.getenv("TRANSCRIBE_LANGUAGE", "vi") or "vi"
        try:
            results = await asyncio.to_thread(
                transcribe_links, links, None, None if language == "auto" else language
            )
            messages = build_telegram_messages(results)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Bóc thoại thất bại.")
            messages = [f"❌ Bóc thoại thất bại: {html.escape(str(exc)[:500])}"]
        for number, text in enumerate(messages, 1):
            try:
                await message.reply_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=_result_markup() if number == len(messages) else None,
                )
            except Exception:  # noqa: BLE001
                LOGGER.exception("Không gửi được kết quả bóc thoại lên Telegram.")
            await asyncio.sleep(0.3)


async def handle_command(message: Any, args: list[str]) -> None:
    """/thoai [links] — with links: transcribe now; without: open link mode."""
    if args and await handle_text(message, " ".join(args)):
        return
    await start_link_mode(message)


async def handle_callback(query: Any) -> None:
    action = str(query.data or "")[len(CALLBACK_PREFIX):]
    await query.answer()
    if action == "done":
        cancel(query.message.chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        await query.message.reply_text("👌 Đã thoát chế độ bóc thoại TikTok.")
