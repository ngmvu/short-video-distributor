"""Telegram "🗓 Lịch đăng" (/lich) — daily auto-publish slots for YouTube.

Config view: on/off, post times (➕ add by typing "11:30 19:00", tap a time to remove), how long
before the slot Gemini writes ideas, ideas per slot, optional topic, remove logo ✦.

tick(bot) runs every minute in the bot process: when a slot's generation time comes, Gemini writes
ideas with SKILL v16.1 and each is sent with "🗓 Duyệt → đăng HH:MM". Approving queues it for Flow;
after rendering, main._publish_scheduled_item uploads it to YouTube for that slot. If nothing is
approved within auto_approve_minutes, tick approves the idea with the best Gemini viral_score.

Callback data: "lich:<action>[:<arg>]"
"""
from __future__ import annotations

import asyncio
import html
import logging
import uuid
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from core import idea_bank
from core import publish_schedule as ps
from core.storage import append_pending_draft, enqueue_draft_with_options

LOGGER = logging.getLogger("short_video_distributor.schedule_bot")
COMMANDS = ["lich", "lich_dang", "schedule"]
CALLBACK_PREFIX = "lich:"
_waiting: dict[str, str] = {}  # chat_id -> "slots" | "topic"
_running: set[str] = set()  # publish keys being generated (double-tap / double-tick guard)
_background_tasks: set[asyncio.Task[None]] = set()
GENERATE_ATTEMPTS = 3  # Gemini 503/504 overloads usually clear within minutes
RETRY_DELAY_SECONDS = 600


def _auto_label(minutes: Any) -> str:
    minutes = int(minutes or 0)
    if minutes <= 0:
        return "Tắt"
    return f"{minutes // 60} tiếng" if minutes % 60 == 0 else f"{minutes} phút"


def _key(publish_at: datetime) -> str:
    return publish_at.strftime("%Y%m%d%H%M")


def _from_key(key: str) -> datetime:
    return datetime.strptime(key, "%Y%m%d%H%M")


def is_waiting(chat_id: str | int) -> bool:
    return str(chat_id) in _waiting


def cancel(chat_id: str | int) -> None:
    _waiting.pop(str(chat_id), None)


# ---------------------------------------------------------------- config view

def config_text(config: dict[str, Any], now: datetime | None = None) -> str:
    on = config.get("enabled")
    slots = ", ".join(config.get("slots", [])) or "chưa có — bấm ➕ Thêm giờ"
    lines = [
        "🗓 <b>Lịch đăng YouTube tự động</b> — " + ("🟢 ĐANG BẬT" if on else "⚪ ĐANG TẮT"),
        f"⏰ Giờ đăng mỗi ngày: <b>{html.escape(slots)}</b>",
        f"✍️ Gemini viết ý tưởng trước giờ đăng: <b>{config.get('lead_hours')} tiếng</b>",
        f"💡 Số ý tưởng mỗi khung: <b>{config.get('ideas_per_slot')}</b> (bạn chọn 1 để duyệt)",
        f"🏷 Chủ đề: <b>{html.escape(config.get('topic') or 'lấy từ 🧠 Kho ý tưởng (/y)')}</b>"
        + ("" if config.get("topic") else f" — còn <b>{idea_bank.stats()['total']}</b> ý chưa dùng"),
        f"🧽 Logo ✦ trước khi đăng: <b>{ps.LOGO_MODE_LABELS[ps.logo_mode(config)]}</b>"
        + (" (che bằng nhãn dán khỉ & chú)" if ps.logo_mode(config) == "sticker" else ""),
        f"🎬 Ghép outro mời đăng ký vào cuối: <b>{'Có' if config.get('append_outro') else 'Không'}</b>",
        f"🤖 Không duyệt sau <b>{_auto_label(config.get('auto_approve_minutes'))}</b> → "
        "tự duyệt ý tưởng Gemini chấm điểm cao nhất"
        if int(config.get("auto_approve_minutes") or 0) > 0
        else "🤖 Tự duyệt khi bạn không bấm: <b>Tắt</b>",
    ]
    upcoming = ps.upcoming_posts(config, now)
    if on and upcoming:
        publish_at, generate_at = upcoming[0]
        lines.append(
            f"\n➡️ Lần tới: viết ý tưởng lúc <b>{generate_at:%H:%M %d/%m}</b> cho bài đăng "
            f"<b>{ps.describe_time(publish_at.isoformat())}</b>."
        )
    lines.append(
        "\nQuy trình: tới giờ → lấy ý từ 🧠 kho (hết kho → trend → chủ đề theo thứ) → Gemini viết theo SKILL v16.1 → bạn bấm 🗓 Duyệt → Flow tạo video → "
        "xóa logo → ghép outro → tải lên YouTube, hẹn công khai đúng giờ. Duyệt trễ sau giờ đăng thì đăng ngay khi xong."
    )
    return "\n".join(lines)


def config_markup(config: dict[str, Any]) -> InlineKeyboardMarkup:
    p = CALLBACK_PREFIX
    rows = [
        [InlineKeyboardButton("⏸ Tắt lịch" if config.get("enabled") else "▶️ Bật lịch", callback_data=f"{p}toggle")],
        [InlineKeyboardButton("➕ Thêm giờ", callback_data=f"{p}add")]
        + [InlineKeyboardButton(f"✖️ {slot}", callback_data=f"{p}del:{slot.replace(':', '')}")
           for slot in config.get("slots", [])[:4]],
    ]
    extra = config.get("slots", [])[4:]
    if extra:
        rows.append([InlineKeyboardButton(f"✖️ {s}", callback_data=f"{p}del:{s.replace(':', '')}") for s in extra])
    rows += [
        [
            InlineKeyboardButton(f"⏱ Trước {config.get('lead_hours')}h", callback_data=f"{p}lead"),
            InlineKeyboardButton(f"💡 {config.get('ideas_per_slot')} ý tưởng", callback_data=f"{p}ideas"),
            InlineKeyboardButton(f"🧽 Logo: {ps.LOGO_MODE_LABELS[ps.logo_mode(config)]}", callback_data=f"{p}logo"),
        ],
        [
            InlineKeyboardButton(f"🤖 Tự duyệt: {_auto_label(config.get('auto_approve_minutes'))}",
                                 callback_data=f"{p}auto"),
            InlineKeyboardButton(f"🎬 Outro: {'Có' if config.get('append_outro') else 'Không'}",
                                 callback_data=f"{p}outro"),
            InlineKeyboardButton("🏷 Chủ đề", callback_data=f"{p}topic"),
        ],
        [
            InlineKeyboardButton("✨ Viết ý tưởng ngay cho khung tới", callback_data=f"{p}now"),
            InlineKeyboardButton("🧠 Kho ý tưởng", callback_data="kho:list"),
        ],
        [InlineKeyboardButton("✅ Xong", callback_data=f"{p}close")],
    ]
    return InlineKeyboardMarkup(rows)


async def send_menu(message: Any) -> None:
    config = ps.load_config()
    await message.reply_text(config_text(config), parse_mode=ParseMode.HTML, reply_markup=config_markup(config))


async def _refresh(query: Any, note: str = "") -> None:
    config = ps.load_config()
    text = config_text(config) + (f"\n\n{note}" if note else "")
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=config_markup(config))
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


def _next(choices: tuple[int, ...], current: Any) -> int:
    try:
        return choices[(choices.index(int(current)) + 1) % len(choices)]
    except (ValueError, TypeError):
        return choices[0]


# ---------------------------------------------------------------- text input

async def handle_text(message: Any, text: str) -> bool:
    state = _waiting.get(str(message.chat_id))
    if not state:
        return False
    if state == "slots":
        slots = ps.parse_slots(text)
        if not slots:
            await message.reply_text("⚠️ Không đọc được giờ. Gửi dạng <code>11:30 19:00</code> (24h).", parse_mode=ParseMode.HTML)
            return True
        config = ps.load_config()
        config = ps.update_config(slots=[*config["slots"], *slots])
        cancel(message.chat_id)
        await message.reply_text(
            config_text(config, ) + f"\n\n✅ Đã thêm {', '.join(slots)}.",
            parse_mode=ParseMode.HTML, reply_markup=config_markup(config),
        )
        return True
    if state == "topic":
        topic = "" if text.strip() in {"-", "xoa", "xóa", "tự do", "tu do"} else text.strip()[:300]
        config = ps.update_config(topic=topic)
        cancel(message.chat_id)
        await message.reply_text(
            config_text(config) + "\n\n✅ Đã lưu chủ đề.", parse_mode=ParseMode.HTML, reply_markup=config_markup(config)
        )
        return True
    return False


# ---------------------------------------------------------------- callbacks

async def handle_callback(query: Any) -> None:
    data = str(query.data or "")[len(CALLBACK_PREFIX):]
    action, _, arg = data.partition(":")
    chat_id = str(query.message.chat_id)
    config = ps.load_config()

    if action == "toggle":
        enabled = not config.get("enabled")
        if enabled and not config.get("slots"):
            await query.answer("Thêm ít nhất 1 giờ đăng trước đã.", show_alert=True)
            return
        ps.update_config(enabled=enabled)
        await query.answer("Đã bật lịch" if enabled else "Đã tắt lịch")
        await _refresh(query)
        return
    if action == "add":
        _waiting[chat_id] = "slots"
        await query.answer()
        await query.message.reply_text(
            "⏰ Gửi giờ đăng (24h), nhiều giờ cách nhau dấu cách, vd: <code>11:30 19:00</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "del":
        slot = f"{arg[:2]}:{arg[2:]}"
        remaining = [s for s in config["slots"] if s != slot]
        ps.update_config(slots=remaining, enabled=bool(config.get("enabled") and remaining))
        await query.answer(f"Đã xóa {slot}")
        await _refresh(query)
        return
    if action == "lead":
        ps.update_config(lead_hours=_next(ps.LEAD_CHOICES, config.get("lead_hours")))
        await query.answer()
        await _refresh(query)
        return
    if action == "ideas":
        ps.update_config(ideas_per_slot=_next(ps.IDEAS_CHOICES, config.get("ideas_per_slot")))
        await query.answer()
        await _refresh(query)
        return
    if action == "auto":
        ps.update_config(auto_approve_minutes=_next(ps.AUTO_APPROVE_CHOICES, config.get("auto_approve_minutes")))
        await query.answer()
        await _refresh(query)
        return
    if action == "outro":
        ps.update_config(append_outro=not config.get("append_outro"))
        await query.answer()
        await _refresh(query)
        return
    if action == "logo":
        modes = ps.LOGO_MODES  # Xóa → Nhãn dán → Giữ nguyên → Xóa …
        mode = modes[(modes.index(ps.logo_mode(config)) + 1) % len(modes)]
        ps.update_config(logo_mode=mode, remove_logo=mode != "none")
        await query.answer()
        await _refresh(query)
        return
    if action == "topic":
        _waiting[chat_id] = "topic"
        await query.answer()
        await query.message.reply_text(
            "🏷 Gửi chủ đề CỐ ĐỊNH cho mọi khung giờ (vd: <i>giá vàng</i>) — sẽ bỏ qua 🧠 Kho ý tưởng.\n"
            "Gửi <code>-</code> để quay về lấy ý từ kho (/y).",
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "now":
        upcoming = ps.upcoming_posts(config)
        if not upcoming:
            await query.answer("Thêm ít nhất 1 giờ đăng trước đã.", show_alert=True)
            return
        publish_at = upcoming[0][0]
        await query.answer(f"✨ Đang viết ý tưởng cho bài {ps.describe_time(publish_at.isoformat())}…")
        ps.mark_generated(publish_at)
        _spawn(generate_for_slot(query.get_bot(), publish_at))
        return
    if action in {"alt", "retry"}:
        await query.answer("✨ Đang viết thêm ý tưởng…")
        _spawn(generate_for_slot(query.get_bot(), _from_key(arg), count=1 if action == "alt" else None))
        return
    if action == "ok":
        await _approve(query, arg)
        return
    if action == "close":
        cancel(chat_id)
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        return
    await query.answer()


async def _approve(query: Any, draft_id: str) -> None:
    from core.tele.approval_bot import _auto_start_video_worker_if_needed, _find_draft_and_index, _format_idea

    draft, index = _find_draft_and_index(draft_id)
    schedule = (draft or {}).get("schedule")
    if not draft or not isinstance(schedule, dict):
        await query.answer("Ý tưởng này không còn khả dụng.", show_alert=True)
        return
    pos = enqueue_draft_with_options(draft_id, publish=False, notify_progress=True)
    if pos <= 0:
        await query.answer("Ý tưởng này không còn khả dụng.", show_alert=True)
        return
    ps.mark_batch_approved(schedule["publish_at"], "manual")
    when = ps.describe_time(schedule["publish_at"])
    await query.answer(f"✅ Đã duyệt — tạo video rồi đăng YouTube {when}")
    label = f"🗓 ĐÃ DUYỆT — hàng đợi #{pos}, đăng YouTube {when}"
    try:
        await query.edit_message_text(
            _format_idea(draft["idea"], index, status_label=label),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None,
        )
    except BadRequest:
        pass
    _auto_start_video_worker_if_needed(publish=False)


# ---------------------------------------------------------------- generation

def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def draft_markup(draft_id: str, publish_at: datetime) -> InlineKeyboardMarkup:
    p = CALLBACK_PREFIX
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🗓 Duyệt → đăng {ps.describe_time(publish_at.isoformat())}",
                                  callback_data=f"{p}ok:{draft_id}")],
            [
                InlineKeyboardButton("🔄 Thêm ý khác", callback_data=f"{p}alt:{_key(publish_at)}"),
                InlineKeyboardButton("❌ Bỏ qua", callback_data=f"skip:{draft_id}"),
            ],
        ]
    )


async def generate_for_slot(bot: Any, publish_at: datetime, count: int | None = None) -> int:
    """Gemini (SKILL v16.1) → drafts carrying the slot → Telegram approval messages."""
    from core.config import get_setting
    from core.gemini.idea_generator import generate_skill_ideas
    from core.tele.approval_bot import _format_idea

    key = _key(publish_at)
    if key in _running:
        return 0
    _running.add(key)
    chat_id = get_setting("TELEGRAM_CHAT_ID")
    config = ps.load_config()
    count = count or int(config.get("ideas_per_slot") or 1)
    when = ps.describe_time(publish_at.isoformat())
    try:
        topic = ps.batch_topic(publish_at) or await asyncio.to_thread(
            idea_bank.choose_topic, publish_at, config.get("topic") or ""
        )
        ideas: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for attempt in range(1, GENERATE_ATTEMPTS + 1):
            try:
                ideas = await asyncio.to_thread(generate_skill_ideas, topic["topic"], count)
                break
            except Exception as exc:  # noqa: BLE001 — Gemini overload (503/504) is usually temporary
                last_error = exc
                LOGGER.warning(
                    "Gemini chưa viết được ý tưởng cho lịch %s (lần %d/%d): %s",
                    when, attempt, GENERATE_ATTEMPTS, exc,
                )
                if attempt < GENERATE_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
        if not ideas:
            exc = last_error or RuntimeError("không rõ lỗi")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Gemini chưa viết được ý tưởng cho bài đăng {when} sau {GENERATE_ATTEMPTS} lần thử: "
                    f"{html.escape(str(exc)[:300])}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔁 Thử lại", callback_data=f"{CALLBACK_PREFIX}retry:{key}")]]
                ),
            )
            return 0
        schedule = ps.schedule_for(publish_at, config)
        draft_ids = [uuid.uuid4().hex[:16] for _ in ideas]
        for draft_id, idea in zip(draft_ids, ideas):
            append_pending_draft(draft_id, idea, source="schedule", schedule=schedule)
        ps.record_batch(publish_at, draft_ids, topic)
        if topic.get("entry_id"):
            idea_bank.mark_used(topic["entry_id"], key)
        for index, (draft_id, idea) in enumerate(zip(draft_ids, ideas), start=1):
            label = (
                f"🗓 Lịch đăng {when} · ⭐ {ps.viral_score(idea):g}/10 · "
                f"{topic.get('label', '')}: {topic['topic'][:120]}"
            )
            await bot.send_message(
                chat_id=chat_id,
                text=_format_idea(idea, index, status_label=label[:300]),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=draft_markup(draft_id, publish_at),
            )
            await asyncio.sleep(0.3)
        LOGGER.info("Đã gửi %d ý tưởng (SKILL v16.1) cho lịch đăng %s.", len(ideas), when)
        return len(ideas)
    finally:
        _running.discard(key)


async def auto_approve_overdue(bot: Any, now: datetime | None = None) -> int:
    """Approve the best-scored pending idea of every slot nobody approved in time."""
    from core.config import get_setting
    from core.storage import DRAFTS_FILE, read_json
    from core.tele.approval_bot import _auto_start_video_worker_if_needed

    config = ps.load_config()
    approved = 0
    for batch in ps.overdue_batches(config, now):
        drafts = [
            d for d in read_json(DRAFTS_FILE, [])
            if isinstance(d, dict) and d.get("id") in batch["draft_ids"]
        ]
        best = ps.pick_best(drafts)
        if best is None:  # every idea was skipped (❌) or already handled
            ps.mark_batch_approved(batch["publish_at"], "none")
            continue
        pos = enqueue_draft_with_options(best["id"], publish=False, notify_progress=True)
        ps.mark_batch_approved(batch["publish_at"], "auto")
        if pos <= 0:
            continue
        approved += 1
        when = ps.describe_time(batch["publish_at"])
        title = html.escape(str(best["idea"].get("title") or best["id"]))
        LOGGER.info("Tự duyệt ý tưởng '%s' cho lịch đăng %s.", best["idea"].get("title"), when)
        try:
            await bot.send_message(
                chat_id=get_setting("TELEGRAM_CHAT_ID"),
                text=(
                    f"🤖 Quá {_auto_label(config.get('auto_approve_minutes'))} chưa duyệt → đã tự duyệt "
                    f"ý tưởng điểm cao nhất: <b>{title}</b> (⭐ {ps.viral_score(best['idea']):g}/10).\n"
                    f"Đang tạo video, sẽ đăng YouTube {when}."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:  # noqa: BLE001
            LOGGER.warning("Không gửi được thông báo tự duyệt lên Telegram.", exc_info=True)
        _auto_start_video_worker_if_needed(publish=False)
    return approved


async def tick(bot: Any, now: datetime | None = None) -> int:
    """Called every minute by the bot's scheduler: generate ideas for slots that are due, and
    auto-approve slots nobody approved in time. Returns how many generations were started."""
    started = 0
    for publish_at in ps.due_posts(ps.load_config(), now):
        ps.mark_generated(publish_at)  # once per slot, even if Gemini fails (a retry button is sent)
        _spawn(generate_for_slot(bot, publish_at))
        started += 1
    await auto_approve_overdue(bot, now)
    return started
