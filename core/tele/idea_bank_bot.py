"""Telegram "🧠 Kho ý tưởng" (/y) — the topics /lich uses, in this order:
✍️ ideas you send → 🔗 situations taken from hot video links → 📈 Google Trends → 📅 weekday theme.

  /y <ý tưởng>          add an idea (a punchline, a situation, anything you heard)
  /y <link TikTok/YT>   transcribe the video, Gemini keeps only the situation, add it
  /y                    show the bank; "➕ Thêm ý" keeps the chat in add mode until ✅ Xong

Callback data: "kho:<action>[:<arg>]"
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

from core import idea_bank as bank

LOGGER = logging.getLogger("short_video_distributor.idea_bank_bot")
COMMANDS = ["y", "kho", "kho_y_tuong"]
CALLBACK_PREFIX = "kho:"
LIST_LIMIT = 12
MAX_LINKS = 5
_waiting: set[str] = set()
_link_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task[None]] = set()


def is_waiting(chat_id: str | int) -> bool:
    return str(chat_id) in _waiting


def cancel(chat_id: str | int) -> None:
    _waiting.discard(str(chat_id))


def _short(text: str, size: int = 110) -> str:
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


def menu_text(note: str = "") -> str:
    entries = bank.load()
    pending = bank.unused(entries)
    counts = bank.stats(entries)
    lines = [
        f"🧠 <b>Kho ý tưởng</b> — <b>{counts['total']}</b> ý chưa dùng "
        f"(✍️ {counts['user']} · 🔗 {counts['link']} · 📈 {counts['trend']})",
        "Tới giờ, 🗓 Lịch đăng lấy ý đầu danh sách. Hết kho thì lấy 1 trend, "
        "rồi tới chủ đề theo thứ trong tuần.",
    ]
    if pending:
        lines.append("")
        for number, entry in enumerate(pending[:LIST_LIMIT], 1):
            icon = bank.SOURCE_LABELS.get(entry["source"], "•").split()[0]
            lines.append(f"{number}. {icon} {html.escape(_short(entry['text']))}")
        if len(pending) > LIST_LIMIT:
            lines.append(f"<i>… và {len(pending) - LIST_LIMIT} ý nữa</i>")
    else:
        lines.append("\n<i>Kho đang trống.</i>")
    lines.append(
        "\n➕ Thêm nhanh: <code>/y câu chốt hoặc tình huống</code>, "
        "hoặc <code>/y https://vt.tiktok.com/...</code> để rút tình huống từ video hot."
    )
    if note:
        lines.append(f"\n{note}")
    return "\n".join(lines)


def menu_markup() -> InlineKeyboardMarkup:
    p = CALLBACK_PREFIX
    pending = bank.unused()[:LIST_LIMIT]
    rows: list[list[InlineKeyboardButton]] = []
    delete_buttons = [
        InlineKeyboardButton(f"✖️ {number}", callback_data=f"{p}del:{entry['id']}")
        for number, entry in enumerate(pending, 1)
    ]
    for start in range(0, len(delete_buttons), 6):
        rows.append(delete_buttons[start:start + 6])
    rows += [
        [
            InlineKeyboardButton("➕ Thêm ý / dán link", callback_data=f"{p}add"),
            InlineKeyboardButton("📈 Lấy 3 trend", callback_data=f"{p}trend"),
        ],
        [InlineKeyboardButton("✅ Xong", callback_data=f"{p}close")],
    ]
    return InlineKeyboardMarkup(rows)


async def send_menu(message: Any, note: str = "") -> None:
    await message.reply_text(menu_text(note), parse_mode=ParseMode.HTML, reply_markup=menu_markup())


async def _refresh(query: Any, note: str = "") -> None:
    try:
        await query.edit_message_text(menu_text(note), parse_mode=ParseMode.HTML, reply_markup=menu_markup())
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# ---------------------------------------------------------------- adding ideas

async def add_text(message: Any, text: str) -> None:
    """Add a typed idea, or start the link → situation job when the text has video links."""
    from core.link_transcriber import extract_video_links

    links, _ = extract_video_links(text)
    if links:
        await _start_link_job(message, links[:MAX_LINKS])
        return
    if len(text.strip()) < 4:
        await message.reply_text("⚠️ Ý tưởng ngắn quá, gửi rõ hơn một chút nhé.")
        return
    entry, created = bank.add(text, "user")
    position = next((i for i, e in enumerate(bank.unused(), 1) if e["id"] == entry["id"]), 0)
    if created:
        reply = f"✅ Đã thêm vào kho (vị trí #{position}): <i>{html.escape(_short(entry['text'], 200))}</i>"
    else:
        reply = "ℹ️ Ý này đã có trong kho rồi."
    if is_waiting(message.chat_id):
        reply += "\nGửi tiếp ý khác, hoặc bấm ✅ Xong."
    await message.reply_text(reply, parse_mode=ParseMode.HTML, reply_markup=_add_mode_markup())


def _add_mode_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🧠 Xem kho", callback_data=f"{CALLBACK_PREFIX}list"),
            InlineKeyboardButton("✅ Xong", callback_data=f"{CALLBACK_PREFIX}close"),
        ]]
    )


async def _start_link_job(message: Any, links: list[str]) -> None:
    queued = " Đang có lượt khác chạy, sẽ chạy ngay sau." if _link_lock.locked() else ""
    await message.reply_text(
        f"⏳ Đang bóc thoại {len(links)} video rồi nhờ Gemini rút tình huống… "
        f"(mỗi video khoảng 1–2 phút).{queued}"
    )
    task = asyncio.create_task(_run_link_job(message, links))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _links_to_ideas(links: list[str]) -> list[str]:
    """Blocking: transcribe each link, extract the situation, add it. Returns reply lines (HTML)."""
    from core.gemini.idea_generator import extract_situation_from_video
    from core.link_transcriber import transcribe_links

    language = os.getenv("TRANSCRIBE_LANGUAGE", "vi") or "vi"
    results = transcribe_links(links, None, None if language == "auto" else language)
    lines = []
    for result in results:
        video = result.video
        name = html.escape(_short(video.title or video.url, 80))
        if result.error:
            lines.append(f"❌ {name}: {html.escape(result.error[:200])}")
            continue
        transcript = " ".join(str(s.get("text", "")).strip() for s in result.segments).strip()
        try:
            situation = extract_situation_from_video(video.title, transcript)
        except Exception as exc:  # noqa: BLE001 — Gemini overload etc.; report per video
            LOGGER.warning("Gemini không rút được tình huống từ %s: %s", video.url, exc)
            lines.append(f"❌ {name}: Gemini chưa rút được tình huống ({html.escape(str(exc)[:150])})")
            continue
        if not situation:
            lines.append(f"🤷 {name}: không thấy tình huống hài nào dùng được.")
            continue
        entry, created = bank.add(
            situation, "link",
            {"url": video.url, "title": video.title, "views": video.view_count},
        )
        if created:
            lines.append(f"✅ {name}\n   → <i>{html.escape(_short(entry['text'], 260))}</i>")
        else:
            lines.append(f"ℹ️ {name}: video / ý này đã có trong kho.")
    return lines


async def _run_link_job(message: Any, links: list[str]) -> None:
    async with _link_lock:
        try:
            lines = await asyncio.to_thread(_links_to_ideas, links)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Rút ý tưởng từ link thất bại.")
            lines = [f"❌ Rút ý tưởng từ link thất bại: {html.escape(str(exc)[:300])}"]
    text = "🔗 <b>Ý tưởng từ link</b>\n" + "\n".join(lines)
    try:
        await message.reply_text(
            text[:3900], parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=_add_mode_markup(),
        )
    except Exception:  # noqa: BLE001
        LOGGER.exception("Không gửi được kết quả kho ý tưởng lên Telegram.")


# ---------------------------------------------------------------- entry points

async def handle_command(message: Any, args: list[str]) -> None:
    """/y [ý tưởng | link] — with text: add it; without: show the bank."""
    text = " ".join(args).strip()
    if text:
        await add_text(message, text)
        return
    await send_menu(message)


async def handle_text(message: Any, text: str) -> bool:
    """In add mode every message is an idea (or links). Returns True when consumed."""
    if not is_waiting(message.chat_id):
        return False
    await add_text(message, text)
    return True


async def handle_callback(query: Any) -> None:
    data = str(query.data or "")[len(CALLBACK_PREFIX):]
    action, _, arg = data.partition(":")
    chat_id = str(query.message.chat_id)
    if action == "add":
        _waiting.add(chat_id)
        await query.answer()
        await query.message.reply_text(
            "➕ Gửi ý tưởng, mỗi tin nhắn một ý: câu chốt, tình huống, chuyện nghe được ngoài đời…\n"
            "Hoặc dán link TikTok / YouTube Shorts / Facebook Reels đang hot để rút tình huống.\n"
            "Bấm ✅ Xong khi gửi xong.",
            reply_markup=_add_mode_markup(),
        )
        return
    if action == "del":
        removed = bank.remove(arg)
        await query.answer("Đã xóa" if removed else "Ý này không còn trong kho")
        await _refresh(query)
        return
    if action == "trend":
        await query.answer("📈 Đang lấy trend…")
        added = await asyncio.to_thread(bank.refill_from_trends, 3)
        note = f"📈 Đã thêm {added} trend." if added else "📈 Không có trend mới hợp kênh (hoặc chưa lấy được Google Trends)."
        await _refresh(query, note)
        return
    if action == "list":
        await query.answer()
        await send_menu(query.message)
        return
    if action == "close":
        cancel(chat_id)
        await query.answer("👌 Đã xong")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        return
    await query.answer()
