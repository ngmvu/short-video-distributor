"""Telegram "🧽 Xóa logo ✦" — remove the Gemini/Veo sparkle from a video, without cropping.

Ways to use it:
  • tap the menu button or /xoalogo, then send a video (as video or file, ≤20 MB — Telegram's bot
    download limit) or a file path / file name from storage/downloads;
  • /xoalogo <đường dẫn hoặc tên file> — process a local file directly;
  • reply /xoalogo to a video message.
The chat stays in this mode until "✅ Xong" / another menu button / /cancel.

Callback data: "xoalogo:done"
"""
from __future__ import annotations

import asyncio
import html
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from core.config import DOWNLOADS_DIR, STORAGE_DIR
from core.tele.notifier import send_video_file
from core.watermark import WatermarkError, remove_gemini_watermark

LOGGER = logging.getLogger("short_video_distributor.watermark_bot")
COMMANDS = ["xoalogo", "xoa_logo", "watermark"]
CALLBACK_PREFIX = "xoalogo:"
UPLOAD_DIR = STORAGE_DIR / "watermark"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Bot API getFile limit
MAX_SEND_BYTES = 50 * 1024 * 1024  # Bot API upload limit
_waiting: set[str] = set()
_job_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task[None]] = set()

PROMPT = (
    "🧽 <b>Xóa logo ✦ Gemini/Veo</b> (không cắt khung, giữ nguyên độ phân giải)\n"
    "📎 Gửi video vào đây (dạng video hoặc file, tối đa 20 MB),\n"
    "hoặc gửi tên / đường dẫn file trong <code>storage/downloads</code>.\n"
    "Bấm ✅ Xong khi không cần nữa."
)


def is_waiting(chat_id: str | int) -> bool:
    return str(chat_id) in _waiting


def cancel(chat_id: str | int) -> None:
    _waiting.discard(str(chat_id))


def _done_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Xong", callback_data=f"{CALLBACK_PREFIX}done")]]
    )


async def start_mode(message: Any) -> None:
    _waiting.add(str(message.chat_id))
    await message.reply_text(PROMPT, parse_mode=ParseMode.HTML, reply_markup=_done_markup())


def resolve_local_video(text: str) -> Path | None:
    """A file name or path pasted in chat → existing .mp4 inside storage/ (None otherwise)."""
    raw = text.strip().strip('"').strip("'").strip()
    if not raw or "\n" in raw or not raw.lower().endswith((".mp4", ".mov", ".webm", ".mkv")):
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = DOWNLOADS_DIR / raw
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    # Only files the bot itself manages: never let chat text point anywhere on disk.
    if not resolved.is_file() or not resolved.is_relative_to(STORAGE_DIR.resolve()):
        return None
    return resolved


def _video_attachment(message: Any) -> Any | None:
    if message is None:
        return None
    if getattr(message, "video", None):
        return message.video
    document = getattr(message, "document", None)
    if document and str(getattr(document, "mime_type", "") or "").startswith("video/"):
        return document
    return None


async def handle_text(message: Any, text: str) -> bool:
    """While waiting: a pasted file name/path. Returns True when the message was consumed."""
    if not is_waiting(message.chat_id):
        return False
    path = resolve_local_video(text)
    if path is None:
        await message.reply_text(
            "⚠️ Không thấy file video đó trong <code>storage/</code>. Gửi video trực tiếp, "
            "hoặc gửi đúng tên file (vd: <code>abc-scene3.mp4</code>).",
            parse_mode=ParseMode.HTML,
            reply_markup=_done_markup(),
        )
        return True
    start_job(message, local_path=path)
    return True


async def handle_video(message: Any, force: bool = False) -> bool:
    """A video/document sent while waiting (or with /xoalogo caption). True when consumed."""
    attachment = _video_attachment(message)
    if attachment is None:
        return False
    caption = str(getattr(message, "caption", "") or "").lower()
    if not (force or is_waiting(message.chat_id) or any(f"/{c}" in caption for c in COMMANDS)):
        return False
    size = int(getattr(attachment, "file_size", 0) or 0)
    if size > MAX_DOWNLOAD_BYTES:
        await message.reply_text(
            f"⚠️ Video {size / 1024 / 1024:.0f} MB, vượt giới hạn 20 MB bot được tải về. "
            "Hãy gửi tên file trong <code>storage/downloads</code> thay vì gửi video.",
            parse_mode=ParseMode.HTML,
        )
        return True
    start_job(message, attachment=attachment)
    return True


async def handle_command(message: Any, args: list[str]) -> None:
    """/xoalogo [file] — with a file: run now; replying to a video: run on it; else open the mode."""
    if args:
        path = resolve_local_video(" ".join(args))
        if path is None:
            await message.reply_text(
                "⚠️ Không thấy file video đó trong <code>storage/</code>.", parse_mode=ParseMode.HTML
            )
            return
        start_job(message, local_path=path)
        return
    reply = getattr(message, "reply_to_message", None)
    if _video_attachment(reply) is not None:
        await handle_video(reply, force=True)
        return
    await start_mode(message)


def start_job(
    message: Any,
    *,
    local_path: Path | None = None,
    attachment: Any = None,
    markup_for: Callable[[Path], InlineKeyboardMarkup | None] | None = None,
) -> None:
    """Run the removal in the background; `markup_for(output)` adds buttons to the result."""
    task = asyncio.create_task(_run_and_reply(message, local_path, attachment, markup_for))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_and_reply(
    message: Any,
    local_path: Path | None,
    attachment: Any,
    markup_for: Callable[[Path], InlineKeyboardMarkup | None] | None = None,
) -> None:
    queued = " (đang có video khác, chạy ngay sau)" if _job_lock.locked() else ""
    status = await message.reply_text(f"⏳ Đang xóa logo ✦…{queued}")
    async with _job_lock:
        try:
            if local_path is None:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                name = Path(str(getattr(attachment, "file_name", "") or "")).stem or "video"
                local_path = UPLOAD_DIR / f"{name[:60]}-{uuid.uuid4().hex[:8]}.mp4"
                tg_file = await attachment.get_file()
                await tg_file.download_to_drive(custom_path=local_path)
            result = await asyncio.to_thread(remove_gemini_watermark, local_path)
        except WatermarkError as exc:
            await _edit(status, f"❌ {html.escape(str(exc))}")
            return
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Xóa logo thất bại.")
            await _edit(status, f"❌ Xóa logo thất bại: {html.escape(str(exc)[:400])}")
            return

    output = result.output
    rel = output.relative_to(STORAGE_DIR.parent) if output.is_relative_to(STORAGE_DIR.parent) else output
    summary = (
        f"✅ Đã xóa logo ✦ ({result.width}×{result.height}, {result.frames} khung, "
        f"độ khớp {result.score:.2f}).\n📁 <code>{html.escape(str(rel))}</code>"
    )
    await _edit(status, summary)
    if output.stat().st_size > MAX_SEND_BYTES:
        await message.reply_text("ℹ️ File lớn hơn 50 MB nên không gửi qua Telegram được, lấy ở đường dẫn trên.")
        return
    markup = markup_for(output) if markup_for else None
    if markup is None and is_waiting(message.chat_id):
        markup = _done_markup()
    try:
        await send_video_file(
            output, f"🧽 {html.escape(output.name)}", message.get_bot(), message.chat_id, markup
        )
        LOGGER.info("Đã gửi video đã xóa logo lên Telegram: %s", output.name)
    except Exception:  # noqa: BLE001
        LOGGER.exception("Không gửi được video đã xóa logo lên Telegram.")
        await message.reply_text("⚠️ Xóa xong nhưng gửi video lên Telegram lỗi; file vẫn ở đường dẫn trên.")


async def _edit(status: Any, text: str) -> None:
    try:
        await status.edit_text(text, parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


async def handle_callback(query: Any) -> None:
    action = str(query.data or "")[len(CALLBACK_PREFIX):]
    await query.answer()
    if action == "done":
        cancel(query.message.chat_id)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        await query.message.reply_text("👌 Đã thoát chế độ xóa logo.")

