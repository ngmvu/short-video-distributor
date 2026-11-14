from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import uuid
from typing import Any

from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    Bot,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from core.config import ROOT_DIR, STORAGE_DIR, get_setting
from core.tele import flow_settings_bot, log_bot, transcribe_bot
from core.gemini.idea_generator import (
    rewrite_idea_twist_or_refine,
    suggest_trending_topics,
)
from core.script_parser import (
    ScriptParseError,
    looks_like_direct_prompt,
    looks_like_script,
    parse_direct_prompt,
    parse_script,
)
from core.storage import (
    APPROVED_QUEUE_FILE,
    DRAFTS_FILE,
    PROCESS_LOCK_FILE,
    append_pending_draft,
    approve_draft,
    enqueue_draft_with_options,
    promote_approved_queue_item,
    read_json,
    save_drafts,
    skip_draft,
)

LOGGER = logging.getLogger("short_video_distributor")

_user_sessions: dict[str, dict[str, Any]] = {}
_trending_topics_cache: dict[str, str] = {}
_queue_run_cache: dict[str, str] = {}
CHOICE_TOPIC_PRESETS: tuple[tuple[str, str], ...] = (
    (
        "📱 Hóng phốt, dính link bẫy",
        "Khỉ chủ tiệm sửa xe và ông chú tò mò hóng một drama đang lan truyền ở bất kỳ đâu (quán cà phê, chợ, đám cưới), "
        "xin link rồi gặp cú lật hài dân gian như bấm nhầm link giả hoặc điện thoại phát loa oang oang khiến ông chú đứng hình chốt 'Đúng là khỉ!'. "
        "Chỉ châm biếm thói hóng hớt; không khai thác clip riêng tư, nội dung nhạy cảm hay khẳng định tin đồn về người thật.",
    ),
    (
        "❓ Hỏi xoáy đáp xoay",
        "Hỏi xoáy đáp xoay hài dân gian ở bối cảnh tự do (bắt chữ nghĩa đen / chơi chữ lươn lẹo): ông chú tra khảo câu đời thường, "
        "khỉ mặt tỉnh queo trả lời bẻ chữ theo nghĩa đen khiến ông chú cạn lời mắng 'Đúng là khỉ!' và khỉ cười vui vẻ hả hê.",
    ),
    (
        "🔥 Khỉ cà khịa ông chú",
        "Ông chú thích sĩ diện, khoe khoang bản lĩnh hoặc chống chế ở bất cứ đâu (đám giỗ, sân bóng, phòng gym); "
        "khỉ bắt bài bằng màn cà khịa lươn lẹo bóc mẽ ngay tại chỗ khiến ông chú quê độ.",
    ),
    (
        "🛠️ Chữa lợn lành thành lợn què",
        "Ông chú nhờ khỉ giúp một việc nhỏ (sửa đồ, nấu ăn, trang trí nhà, chỉ đường); khỉ áp dụng mẹo bá đạo làm một nẻo "
        "khiến kết quả dở khóc dở cười ở bối cảnh tự do.",
    ),
    (
        "💸 Mặc cả & bảng giá bá đạo",
        "Ông chú mặc cả ở chợ, quán nước hoặc dịch vụ gì đó bằng lý lẽ khôn lỏi; khỉ phản đòn bằng "
        "phép tính tiền lươn lẹo khiến chú hết đường cãi.",
    ),
    (
        "🏥 Chuyện công sở, bệnh viện, ngân hàng",
        "Khỉ và ông chú đi làm thủ tục ở công sở, bệnh viện hoặc ngân hàng; chú tự tin làm sai giấy tờ, "
        "khỉ chỉ ra chi tiết ngớ ngẩn ngay trước mắt khiến chú đứng hình.",
    ),
    (
        "📱 Bắt trend mạng xã hội",
        "Đú trend / công nghệ nửa mùa: khỉ và ông chú thử công nghệ mới (livestream bán hàng, AI, định vị, quay tóp tóp) "
        "nhưng thao tác ngược đời dẫn tới cú nổ hài hước; không bịa chuyện về người thật.",
    ),
    (
        "😅 Ông chú sợ nóc nhà",
        "Ông chú vỗ ngực khoe làm chủ gia đình trước mặt khỉ nhưng lập tức đổi giọng ngoan ngoãn khi nóc nhà gọi điện hoặc kiểm tra quỹ đen; "
        "khỉ chớp thời cơ trêu chọc đắc thắng.",
    ),
    (
        "🏘️ Hàng xóm xóm trọ",
        "Khỉ và ông chú bị cuốn vào chuyện hàng xóm như đậu xe chắn cửa, hát karaoke hay mượn đồ không trả; "
        "giải quyết bằng mẹo dân gian lươn lẹo và cú twist bất ngờ.",
    ),
    (
        "🎤 Đấu khẩu bằng thơ",
        "Khỉ và ông chú đấu khẩu bằng câu vần ngắn kiểu ca dao chế dân gian ở bối cảnh tự do; câu đầu nghe đạo lý sâu sắc, "
        "câu sau khỉ bẻ lái châm biếm thói nợ tiền hoặc sợ vợ của chú.",
    ),
    (
        "✈️ Du lịch & đám cưới dở khóc dở cười",
        "Khỉ và ông chú đi du lịch, ăn đám cưới hoặc dự đám giỗ; chú sĩ diện mừng phong bì, đặt phòng hoặc gọi món sai cách, "
        "khỉ bóc mẽ bằng một câu chốt khiến chú há hốc mồm.",
    ),
    (
        "🛒 Chuyện ăn uống đời thường",
        "Khỉ và ông chú tranh luận chuyện chia đồ ăn vặt, bao chầu nhậu hoặc tính tiền quán cóc; "
        "mẹo chia phần khôn lỏi của khỉ ở cảnh cuối khiến chú tức nổ đom đóm mắt.",
    ),
    (
        "🤪 Hỏi ngu hỏi dại",
        "Khỉ hỏi những câu cực ngô nghê nhưng nghe có lý (vì sao gà không biết đi xe máy, trứng có tự đẻ không), "
        "ông chú cố giải thích nghiêm túc rồi tự mắc bẫy logic của chính mình, chốt 'Đúng là khỉ!'.",
    ),
    (
        "🔥 Hót trend hôm nay",
        "Khỉ và ông chú bắt một trend/meme đang hot trên mạng (câu nói viral, thử thách, filter, nhạc trend) ở bối cảnh tự do; "
        "ông chú đú sai cách, khỉ cà khịa. Chỉ dùng trend chung chung, hư cấu, không nhắc người thật hay tin đồn.",
    ),
    (
        "🤖 Hỏi AI, hỏi Google",
        "Ông chú tin sái cổ câu trả lời của AI/chatbot/Google (thực đơn, đường đi, mẹo vặt), khỉ làm theo đúng từng chữ "
        "dẫn tới kết quả dở khóc dở cười.",
    ),
    (
        "💼 Phỏng vấn xin việc",
        "Khỉ làm nhà tuyển dụng, ông chú đi xin việc khoe thành tích 'nổ' tưng bừng; khỉ hỏi một câu xoáy bóc mẽ ngay tại chỗ.",
    ),
    (
        "💘 Tỏ tình & xem mắt",
        "Ông chú nhờ khỉ làm quân sư tỏ tình hoặc đi xem mắt hộ; khỉ chỉ chiêu bá đạo khiến buổi hẹn rẽ sang hướng không ai ngờ.",
    ),
    (
        "🍜 Review quán xá, ăn uống",
        "Khỉ và ông chú đi review quán ăn, cà phê, trà sữa kiểu 'food reviewer'; chú chê bai làm màu, "
        "khỉ phát hiện chú vừa chê vừa vét sạch đĩa.",
    ),
    (
        "⛽ Giá cả, lương thưởng, vé Tết",
        "Chuyện ví tiền thường ngày (giá xăng, giá vàng, vé xe Tết, lì xì, tiền điện) kể bằng giọng hài: ông chú than nghèo kể khổ, "
        "khỉ tính toán lươn lẹo khiến chú tắt đài. Chỉ hài đời thường, không số liệu thật.",
    ),
    (
        "🧙 Thầy bói & mê tín vui",
        "Khỉ làm thầy bói/thầy phong thủy tay ngang, phán một câu mập mờ, ông chú hoảng hồn làm theo rồi nhận ra bị 'dắt mũi'; "
        "giữ tông hài nhẹ nhàng, không đả kích tín ngưỡng.",
    ),
    (
        "⚽ Bóng đá & thi đấu hài hước",
        "Khỉ và ông chú xem bóng đá, chơi cầu lông, đá cờ hoặc thi đấu phong trào; chú tự nhận mình là cao thủ nhưng thua khỉ ở chi tiết nhỏ nhất.",
    ),
)

TELEGRAM_BOT_COMMANDS = [
    BotCommand("gemini", "Đưa ý tưởng → Gemini gợi ý kịch bản → duyệt"),
    BotCommand("choice", "Lấy ý tưởng kịch bản từ Gemini"),
    BotCommand("new", "Dán kịch bản có sẵn để tạo video"),
    BotCommand("direct", "Dán prompt Flow trực tiếp, không dùng Gemini"),
    BotCommand("hang_doi", "Xem các kịch bản đang chờ render"),
    BotCommand("quality", "Chọn chất lượng tải video 720p / 1080p"),
    BotCommand("flow", "Xem / đổi cấu hình Google Flow"),
    BotCommand("thoai", "Bóc lời thoại từ link video TikTok"),
    BotCommand("log", "Xem log: crawl thoại, lỗi, mới nhất"),
]


async def register_bot_commands(bot: Bot) -> None:
    """Replace default and configured-chat menus with supported bot actions."""
    await bot.set_my_commands(TELEGRAM_BOT_COMMANDS)
    chat_id = get_setting("TELEGRAM_CHAT_ID")
    await bot.set_my_commands(
        TELEGRAM_BOT_COMMANDS,
        scope=BotCommandScopeChat(chat_id=chat_id),
    )


def _opt_setting(key: str, default: str) -> str:
    try:
        val = get_setting(key, default)
        return val.strip() if isinstance(val, str) and val.strip() else default
    except (KeyError, ValueError):
        return default


def _character_pairs() -> list[tuple[str, str]]:
    char1_tag = _opt_setting("CHARACTER_1_TAG", "@khi")
    char1_voice = _opt_setting("CHARACTER_1_VOICE", "@Voice_Khi")
    char2_tag = _opt_setting("CHARACTER_2_TAG", "@nguoi")
    char2_voice = _opt_setting("CHARACTER_2_VOICE", "@Voice_Nguoi")
    return [(char1_tag, char1_voice), (char2_tag, char2_voice)]


def _is_authorized_chat(chat_id: str | int | None) -> bool:
    try:
        configured = get_setting("TELEGRAM_CHAT_ID")
        return str(chat_id or "").strip() == str(configured).strip()
    except Exception:
        return False


def _get_session(chat_id: str | int) -> dict[str, Any]:
    cid = str(chat_id)
    if cid not in _user_sessions:
        _user_sessions[cid] = {"state": "idle", "theme": ""}
    return _user_sessions[cid]


def _reset_session(chat_id: str | int) -> None:
    _user_sessions[str(chat_id)] = {"state": "idle", "theme": ""}


def _resolve_scene_speaker(scene: dict[str, Any]) -> str:
    voice = str(scene.get("voice") or "").strip()
    first_frame = str(scene.get("first_frame") or "").strip()
    prompt = str(scene.get("prompt") or "")

    if "Speaker:" in prompt:
        after_spk = prompt.split("Speaker:", 1)[1].strip()
        spk_tag = after_spk.split()[0] if after_spk else ""
        if "@khi" in spk_tag.lower():
            return "🐒 Khỉ chủ tiệm (@khi)"
        if "@nguoi" in spk_tag.lower():
            return "👨 Ông chú (@nguoi)"
        if spk_tag:
            return spk_tag

    if voice:
        if "khi" in voice.lower():
            return "🐒 Khỉ chủ tiệm (@khi)"
        if "nguoi" in voice.lower():
            return "👨 Ông chú (@nguoi)"
        return voice

    if first_frame:
        tags = [t for t in re.findall(r"@[\w.]+", first_frame) if not t.lower().startswith("@voice")]
        if tags:
            first_char = tags[0].lower()
            if "khi" in first_char:
                return "🐒 Khỉ chủ tiệm (@khi)"
            if "nguoi" in first_char:
                return "👨 Ông chú (@nguoi)"
            return tags[0]

    return "Nhân vật"


def _resolve_scene_text(scene: dict[str, Any]) -> tuple[str, str]:
    speaker = _resolve_scene_speaker(scene)
    dialogue = str(scene.get("dialogue") or "").strip()
    if dialogue:
        return speaker, f'"{dialogue}"'

    prompt_text = str(scene.get("prompt") or "").strip()
    quotes = re.findall(r'"([^"]+)"', prompt_text)
    if quotes:
        return speaker, f'"{quotes[0]}"'

    if scene.get("scene_title"):
        return speaker, str(scene["scene_title"]).strip()

    return speaker, prompt_text


def _format_idea(
    idea: dict[str, Any],
    index: int,
    status_label: str | None = None,
) -> str:
    title = escape_markdown(idea.get("title", f"Kịch bản {index}")[:80], version=2)
    header = f"🎬 *Kịch bản {index}: {title}*"
    if status_label:
        header += f" — *{escape_markdown(status_label, version=2)}*"
    lines = [header]

    twist = idea.get("twist_highlight") or idea.get("twist_summary")
    if twist:
        lines.append(f"\n💡 *Cốt truyện:* {escape_markdown(str(twist)[:350], version=2)}")
    elif idea.get("setting"):
        lines.append(f"\n📍 *Bối cảnh:* {escape_markdown(str(idea['setting'])[:200], version=2)}")

    scenes = [s for s in idea.get("scenes", []) if isinstance(s, dict)]
    if scenes:
        lines.append("\n🎭 *Nội dung các cảnh:*")
        for scene_index, scene in enumerate(scenes, start=1):
            dur = str(scene.get("duration") or "").strip()
            if dur and dur != "auto":
                raw_label = f"Scene {scene_index} ({dur})"
            else:
                raw_label = f"Scene {scene_index}"
            scene_label = escape_markdown(raw_label, version=2)
            speaker, text = _resolve_scene_text(scene)
            esc_speaker = escape_markdown(speaker, version=2)
            esc_text = escape_markdown(text, version=2)
            lines.append(f"• *{scene_label}:* {esc_speaker}: {esc_text}")

    viral_quote = str(idea.get("viral_quote") or "").strip().strip('"')
    if viral_quote:
        lines.append(f"\n🔥 *Thoại viral:* \"{escape_markdown(viral_quote[:150], version=2)}\"")

    if status_label:
        lines.append(f"\n{escape_markdown(status_label, version=2)}")

    return "\n".join(lines)[:4000]


def _find_draft_and_index(draft_id: str) -> tuple[dict[str, Any] | None, int]:
    drafts = read_json(DRAFTS_FILE, [])
    if not isinstance(drafts, list):
        return None, 1
    for index, item in enumerate(drafts, start=1):
        if isinstance(item, dict) and item.get("id") == draft_id:
            return item, index
    return None, 1


async def send_draft_ideas(
    ideas: list[dict[str, Any]], bot: Bot | None = None
) -> None:
    chat_id = get_setting("TELEGRAM_CHAT_ID")
    new_drafts = [
        {"id": uuid.uuid4().hex[:16], "idea": idea, "state": "pending"}
        for idea in ideas
    ]
    existing = read_json(DRAFTS_FILE, [])
    if isinstance(existing, list):
        combined = [
            item
            for item in existing
            if isinstance(item, dict)
            and item.get("state") in {"approved", "processed"}
        ][-30:]
        combined.extend(new_drafts)
    else:
        combined = new_drafts
    save_drafts(combined)

    if bot is None:
        async with Bot(get_setting("TELEGRAM_BOT_TOKEN")) as standalone_bot:
            await _send_drafts(standalone_bot, chat_id, new_drafts)
        return
    await _send_drafts(bot, chat_id, new_drafts)


async def send_existing_drafts(bot: Bot | None = None) -> int:
    chat_id = get_setting("TELEGRAM_CHAT_ID")
    drafts = read_json(DRAFTS_FILE, [])
    if not isinstance(drafts, list) or not drafts:
        raise ValueError("Chưa có ý tưởng nào được lưu trong storage/drafts.json.")

    reusable = [
        item
        for item in drafts
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("idea"), dict)
    ]
    if not reusable:
        raise ValueError("Không có bản nháp hợp lệ trong storage/drafts.json.")

    for item in reusable:
        if item.get("state") == "skipped":
            item["state"] = "pending"
    save_drafts(reusable)

    if bot is None:
        async with Bot(get_setting("TELEGRAM_BOT_TOKEN")) as standalone_bot:
            await _send_drafts(standalone_bot, chat_id, reusable)
        return len(reusable)
    await _send_drafts(bot, chat_id, reusable)
    return len(reusable)


async def _send_drafts(
    bot: Bot, chat_id: str, drafts: list[dict[str, Any]]
) -> None:
    for index, draft in enumerate(drafts, start=1):
        is_approved = draft.get("state") == "approved"
        if is_approved:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"✅ Đã Approve kịch bản {index}",
                            callback_data=f"approved_info:{draft['id']}",
                        ),
                    ]
                ]
            )
            status_label = "✅ ĐÃ APPROVE — Đang trong hàng đợi tạo video"
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"🎬 Duyệt → Gửi Tele",
                            callback_data=f"prod_tele:{draft['id']}",
                        ),
                        InlineKeyboardButton(
                            f"🚀 Duyệt → Đăng MXH",
                            callback_data=f"prod_pub:{draft['id']}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🔄 Đổi twist khác",
                            callback_data=f"regen_twist:{draft['id']}",
                        ),
                        InlineKeyboardButton(
                            "❌ Bỏ qua", callback_data=f"skip:{draft['id']}"
                        ),
                    ],
                ]
            )
            status_label = None
        LOGGER.info("Sending Telegram approval draft %s/%s.", index, len(drafts))
        for attempt in range(1, 4):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=_format_idea(draft["idea"], index, status_label=status_label),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=keyboard,
                )
                break
            except (TimedOut, NetworkError) as exc:
                if attempt == 3:
                    raise
                LOGGER.warning(
                    "Telegram timeout/network error sending draft %s/%s (attempt %s/3): %s. Retrying...",
                    index,
                    len(drafts),
                    attempt,
                    exc,
                )
                await asyncio.sleep(2.5 * attempt)
        LOGGER.info("Telegram accepted approval draft %s/%s.", index, len(drafts))
        await asyncio.sleep(0.3)


async def send_auth_error_notification(bot: Bot | None = None) -> None:
    chat_id = get_setting("TELEGRAM_CHAT_ID")
    text = (
        "⛔ Phiên đăng nhập Google Flow (auth.json) đã hết hạn hoặc chưa mở được Project.\n"
        "Hệ thống đã DỪNG tạo video (các kịch bản đã Approve vẫn được giữ nguyên trong hàng đợi).\n\n"
        "👉 Bấm nút bên dưới hoặc trên Dashboard để chạy save_flow_auth.py và đăng nhập lại:"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔑 Chạy save_flow_auth.py (Đăng nhập lại Flow)",
                    callback_data="run_save_flow_auth:now",
                )
            ]
        ]
    )
    if bot is not None:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        return
    async with Bot(get_setting("TELEGRAM_BOT_TOKEN")) as standalone_bot:
        await standalone_bot.send_message(
            chat_id=chat_id, text=text, reply_markup=keyboard
        )


def _auto_start_video_worker_if_needed(publish: bool = False) -> None:
    from filelock import FileLock, Timeout

    try:
        lock = FileLock(str(PROCESS_LOCK_FILE), timeout=0)
        with lock:
            pass
    except Timeout:
        LOGGER.info(
            "Tiến trình tạo video đã đang chạy ngầm trên server, không cần khởi động lại."
        )
        return

    executable = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if not executable.is_file():
        executable = sys.executable
    cmd_mode = "--process-queue" if publish else "--produce-telegram"
    log_file = STORAGE_DIR / "dashboard.log"
    try:
        with log_file.open("ab", buffering=0) as out:
            subprocess.Popen(
                [str(executable), str(ROOT_DIR / "main.py"), cmd_mode],
                cwd=ROOT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        LOGGER.info(
            "🚀 Tự động khởi chạy worker ngầm %s để tạo video ngay sau khi Approve!",
            cmd_mode,
        )
    except Exception as exc:
        LOGGER.warning("Không thể tự khởi động worker ngầm: %s", exc)


async def _run_background_regen_twist(
    bot: Bot, chat_id: str, message: Any, draft: dict[str, Any], index: int
) -> None:
    draft_id = draft["id"]
    old_idea = draft["idea"]
    try:
        new_idea = await asyncio.to_thread(rewrite_idea_twist_or_refine, old_idea, True)
        draft["idea"] = new_idea
        existing = read_json(DRAFTS_FILE, [])
        if isinstance(existing, list):
            for d in existing:
                if isinstance(d, dict) and d.get("id") == draft_id:
                    d["idea"] = new_idea
                    break
            save_drafts(existing)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🎬 Duyệt → Gửi Tele", callback_data=f"prod_tele:{draft_id}"
                    ),
                    InlineKeyboardButton(
                        "🚀 Duyệt → Đăng MXH", callback_data=f"prod_pub:{draft_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔄 Đổi twist khác nữa", callback_data=f"regen_twist:{draft_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Bỏ qua", callback_data=f"skip:{draft_id}"
                    ),
                ],
            ]
        )
        if message:
            await message.edit_text(
                text=_format_idea(new_idea, index, status_label="✨ ĐÃ ĐỔI TWIST MỚI"),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
    except Exception as exc:
        LOGGER.exception("Error rewriting twist: %s", exc)
        if message:
            await message.reply_text(f"⚠️ Không thể đổi twist lúc này: {exc}")


async def _run_background_refine_script(
    bot: Bot, chat_id: str, message: Any, draft: dict[str, Any], index: int
) -> None:
    draft_id = draft["id"]
    old_idea = draft["idea"]
    try:
        new_idea = await asyncio.to_thread(rewrite_idea_twist_or_refine, old_idea, False)
        draft["idea"] = new_idea
        existing = read_json(DRAFTS_FILE, [])
        if isinstance(existing, list):
            for d in existing:
                if isinstance(d, dict) and d.get("id") == draft_id:
                    d["idea"] = new_idea
                    break
            save_drafts(existing)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🎬 Tạo video → gửi Tele", callback_data=f"prod_tele:{draft_id}"
                    ),
                    InlineKeyboardButton(
                        "🚀 Tạo + Đăng MXH", callback_data=f"prod_pub:{draft_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔄 Đổi twist khác", callback_data=f"regen_twist:{draft_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Hủy bỏ", callback_data=f"skip:{draft_id}"
                    ),
                ],
            ]
        )
        if message:
            await message.edit_text(
                text=_format_idea(
                    new_idea, index, status_label="💡 GEMINI ĐÃ TỐI ƯU THOẠI & TWIST"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
    except Exception as exc:
        LOGGER.exception("Error refining script: %s", exc)
        if message:
            await message.reply_text(f"⚠️ Không thể tối ưu lúc này: {exc}")


async def handle_approval_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    if str(query.message.chat_id if query.message else "") != get_setting(
        "TELEGRAM_CHAT_ID"
    ):
        await query.answer("Bạn không có quyền sử dụng nút này.", show_alert=True)
        return

    action, _, draft_id = query.data.partition(":")
    if action in {"direct_toggle", "direct_confirm", "direct_cancel"}:
        message = query.message
        session = _get_session(message.chat_id if message else "")
        parts = query.data.split(":")
        collection_id = parts[1] if len(parts) > 1 else ""
        if (
            message is None
            or session.get("state") != "selecting_direct_scenes"
            or session.get("direct_collection_id") != collection_id
        ):
            await query.answer("Phiên chọn cảnh đã hết hạn.", show_alert=True)
            return

        scenes = session["direct_scenes"]
        selected = session["direct_selected"]
        if action == "direct_toggle":
            try:
                scene_index = int(parts[2])
            except (IndexError, ValueError):
                await query.answer("Cảnh không hợp lệ.", show_alert=True)
                return
            if not 0 <= scene_index < len(scenes):
                await query.answer("Cảnh không hợp lệ.", show_alert=True)
                return
            if scene_index in selected:
                selected.remove(scene_index)
            else:
                selected.add(scene_index)
            await query.edit_message_text(
                text=_direct_scene_selection_text(scenes, selected),
                reply_markup=_direct_scene_selection_markup(
                    collection_id, scenes, selected
                ),
            )
            await query.answer()
            return

        if action == "direct_cancel":
            _reset_session(message.chat_id)
            await query.answer("Đã hủy.")
            await query.edit_message_text("👌 Đã hủy chọn cảnh.")
            await message.reply_text(
                "Bạn có thể bắt đầu lại từ menu bên dưới.",
                reply_markup=main_menu_keyboard(),
            )
            return

        chosen_scenes = [
            scene for index, scene in enumerate(scenes) if index in selected
        ]
        if not chosen_scenes:
            await query.answer("Hãy chọn ít nhất một cảnh.", show_alert=True)
            return

        idea = {
            "title": "Prompt trực tiếp từ Telegram",
            "source": "telegram_direct",
            "scenes": chosen_scenes,
        }
        _reset_session(message.chat_id)
        await query.answer("Đã xác nhận các cảnh.")
        await query.edit_message_text(
            f"✅ Đã xác nhận {len(chosen_scenes)} cảnh. Chọn cách tạo video bên dưới."
        )
        await _send_script_draft_preview(
            message,
            idea,
            source="telegram_direct",
            allow_refine=False,
        )
        await message.reply_text(
            "Menu chính:",
            reply_markup=main_menu_keyboard(),
        )
        return

    if action == "set_quality":
        from core.config import set_runtime_pref

        quality = "1080" if draft_id == "1080" else "720"
        set_runtime_pref("FLOW_DOWNLOAD_QUALITY", quality)
        await query.answer(f"Đã chọn tải video {quality}p")
        try:
            await query.edit_message_text(
                f"✅ Chất lượng tải video Flow: *{quality}p* (áp dụng cho các video tiếp theo).",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        return

    if action == "run_save_flow_auth":
        executable = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        if not executable.is_file():
            executable = sys.executable
        try:
            subprocess.Popen(
                [
                    str(executable),
                    str(ROOT_DIR / "scripts" / "save_flow_auth.py"),
                    "--auto-save",
                ],
                cwd=ROOT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await query.answer(
                "🔑 Đã mở trình duyệt đăng nhập Flow (save_flow_auth.py) trên máy tính!",
                show_alert=True,
            )
        except Exception as exc:
            LOGGER.exception("Could not launch save_flow_auth.py from Telegram.")
            await query.answer(f"Không mở được trình duyệt: {exc}", show_alert=True)
        return

    if action == "approved_info":
        await query.answer(
            "✅ Kịch bản này đã được Approve và đang trong hàng đợi tạo video!",
            show_alert=False,
        )
        return

    if action == "queue_run":
        from filelock import FileLock, Timeout

        item_id = _queue_run_cache.get(draft_id)
        if not item_id:
            await query.answer("Nút chọn hàng đợi đã hết hạn. Mở lại /hang_doi.", show_alert=True)
            return

        try:
            with FileLock(str(PROCESS_LOCK_FILE), timeout=0):
                pass
        except Timeout:
            await query.answer(
                "Hàng đợi đang chạy. Đợi chạy xong rồi chọn mục muốn chạy trước nhé.",
                show_alert=True,
            )
            return

        if promote_approved_queue_item(item_id) == 0:
            await query.answer("Mục này không còn trong hàng đợi.", show_alert=True)
            return
        _queue_run_cache.pop(draft_id, None)
        queue = read_json(APPROVED_QUEUE_FILE, [])
        await query.answer("Đã đưa kịch bản lên đầu hàng đợi và bắt đầu chạy.")
        if query.message:
            await _send_queue_status(query.message)
        _auto_start_video_worker_if_needed(
            publish=any(
                isinstance(item, dict) and bool(item.get("publish"))
                for item in queue
            )
        )
        return

    if action == "menu" and draft_id != "cancel":
        await query.answer(
            "⏳ Đang gọi Gemini lấy gợi ý chủ đề..." if draft_id == "suggest_topics" else None
        )
        if query.message:
            await _run_menu_action(query.message, draft_id)
        return

    if action == "menu":
        chat_id = str(query.message.chat_id if query.message else "")
        if draft_id == "cancel":
            _reset_session(chat_id)
            await query.answer("Đã hủy.")
            if query.message:
                try:
                    await query.edit_message_text("❌ Đã hủy thao tác.")
                except Exception:
                    pass
            return

    if action == "pick_topic":
        topic = _trending_topics_cache.get(draft_id, "")
        if not topic:
            await query.answer("Chủ đề không còn khả dụng.", show_alert=True)
            return
        topic_label, source_separator, source_note = topic.partition(
            "\nNguồn tham khảo: "
        )
        chat_id = str(query.message.chat_id if query.message else "")
        session = _get_session(chat_id)
        session["theme"] = topic
        session.pop("brief_type", None)
        await query.answer(f"Đã chọn: {topic_label[:25]}...")
        if query.message:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⚡ 3 kịch bản", callback_data="gen_count:3"),
                        InlineKeyboardButton("⭐ 5 kịch bản", callback_data="gen_count:5"),
                        InlineKeyboardButton("🔥 10 kịch bản", callback_data="gen_count:10"),
                    ],
                    [
                        InlineKeyboardButton("❌ Hủy", callback_data="menu:cancel"),
                    ],
                ]
            )
            source_details = (
                f"\n\n{escape_markdown('Nguồn tham khảo: ' + source_note, version=2)}"
                if source_separator
                else ""
            )
            await query.message.reply_text(
                f"💡 *Chủ đề đã chọn:*\n_{escape_markdown(topic_label, version=2)}_"
                f"{source_details}\n\n"
                "👉 Hãy chọn số lượng kịch bản bạn muốn Gemini tạo:",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
        return

    if action == "gen_count":
        count = int(draft_id) if draft_id.isdigit() else 3
        chat_id = str(query.message.chat_id if query.message else "")
        session = _get_session(chat_id)
        theme = session.get("theme", "").strip()
        display_brief = theme
        if session.get("brief_type") == "situation" and theme:
            theme = (
                "TÌNH HUỐNG GỐC CẦN GEMINI CHỈNH SỬA VÀ PHÁT TRIỂN:\n"
                f"{theme}\n"
                "Giữ nguyên mấu chốt gây cười của tình huống gốc, làm rõ logic và "
                "phát triển thành các biến thể kịch bản khác nhau nhưng vẫn bám sát "
                "tình huống này; không đổi sang chủ đề không liên quan."
            )
        _reset_session(chat_id)
        await query.answer(f"⏳ Bắt đầu tạo {count} ý tưởng...")
        if query.message:
            await query.edit_message_text(
                f"🤖 *Đang gọi Gemini tạo {count} kịch bản cho chủ đề:*\n_{display_brief or 'Hài đời thường bắt trend'}_\n\n"
                "⏳ Vui lòng đợi trong giây lát, các kịch bản sẽ được gửi đến bên dưới kèm nút Duyệt...",
                parse_mode=ParseMode.MARKDOWN,
            )
            asyncio.create_task(
                _run_background_ideas_generation(
                    context.bot,
                    chat_id,
                    theme,
                    count,
                )
            )
        return

    if action in {"prod_tele", "prod_pub"}:
        publish = action == "prod_pub"
        pos = enqueue_draft_with_options(draft_id, publish=publish, notify_progress=True)
        label_action = "Tạo video & Đăng MXH" if publish else "Tạo video & gửi Telegram"
        message = (
            f"✅ Đã đưa vào hàng đợi #{pos} ({label_action})!"
            if pos > 0
            else "Kịch bản không còn khả dụng."
        )
        await query.answer(message)
        if query.message and pos > 0:
            draft, index = _find_draft_and_index(draft_id)
            if draft and isinstance(draft.get("idea"), dict):
                icon = "🚀" if publish else "🎬"
                badge = f"{icon} ĐÃ DUYỆT (Hàng đợi #{pos}) — {label_action}"
                updated_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                f"{icon} Đang trong hàng đợi #{pos}",
                                callback_data=f"approved_info:{draft_id}",
                            )
                        ]
                    ]
                )
                await query.edit_message_text(
                    text=_format_idea(
                        draft["idea"],
                        index,
                        status_label=badge,
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=updated_keyboard,
                )
            _auto_start_video_worker_if_needed(publish=publish)
        return

    if action == "regen_twist":
        draft, index = _find_draft_and_index(draft_id)
        if not draft or not isinstance(draft.get("idea"), dict):
            await query.answer("Kịch bản không còn khả dụng.", show_alert=True)
            return
        await query.answer("⏳ Đang gọi Gemini đổi cú twist mới...")
        chat_id = str(query.message.chat_id if query.message else "")
        asyncio.create_task(
            _run_background_regen_twist(
                context.bot, chat_id, query.message, draft, index
            )
        )
        return

    if action == "refine_script":
        draft, index = _find_draft_and_index(draft_id)
        if not draft or not isinstance(draft.get("idea"), dict):
            await query.answer("Kịch bản không còn khả dụng.", show_alert=True)
            return
        await query.answer("⏳ Đang gọi Gemini tối ưu thoại & twist...")
        chat_id = str(query.message.chat_id if query.message else "")
        asyncio.create_task(
            _run_background_refine_script(
                context.bot, chat_id, query.message, draft, index
            )
        )
        return

    draft, index = _find_draft_and_index(draft_id)
    if action == "approve":
        approved = approve_draft(draft_id, allow_requeue=True)
        message = (
            "✅ Đã Approve, đang đưa vào hàng đợi sản xuất!"
            if approved
            else "Kịch bản không còn khả dụng."
        )
        await query.answer(message)
        if query.message:
            if approved and draft and isinstance(draft.get("idea"), dict):
                updated_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                f"✅ Đã Approve kịch bản {index}",
                                callback_data=f"approved_info:{draft_id}",
                            )
                        ]
                    ]
                )
                await query.edit_message_text(
                    text=_format_idea(
                        draft["idea"],
                        index,
                        status_label="✅ ĐÃ APPROVE — Đang trong hàng đợi tạo video",
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=updated_keyboard,
                )
                _auto_start_video_worker_if_needed(publish=False)
            else:
                original_text = (query.message.text or "").strip()
                combined = (
                    f"{original_text}\n\n{message}" if original_text else message
                )
                await query.edit_message_text(text=combined)
        return

    if action == "skip":
        skipped = skip_draft(draft_id)
        message = (
            "❌ Đã bỏ qua kịch bản."
            if skipped
            else "Kịch bản không còn khả dụng."
        )
        await query.answer(message)
        if query.message:
            if skipped and draft and isinstance(draft.get("idea"), dict):
                updated_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                f"🔄 Duyệt lại kịch bản {index}",
                                callback_data=f"approve:{draft_id}",
                            )
                        ]
                    ]
                )
                await query.edit_message_text(
                    text=_format_idea(
                        draft["idea"],
                        index,
                        status_label="❌ ĐÃ BỎ QUA",
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=updated_keyboard,
                )
            else:
                original_text = (query.message.text or "").strip()
                combined = (
                    f"{original_text}\n\n{message}" if original_text else message
                )
                await query.edit_message_text(text=combined)
        return

    await query.answer("Lệnh không hợp lệ.", show_alert=True)


async def _send_queue_status(message: Any) -> None:
    queue = read_json(APPROVED_QUEUE_FILE, [])
    _queue_run_cache.clear()
    if not isinstance(queue, list) or not queue:
        await message.reply_text(
            "📋 *Hàng đợi duyệt hiện đang trống.*\nChưa có video nào chờ sản xuất.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    lines = [f"📋 *Hàng đợi sản xuất video ({len(queue)} kịch bản):*"]
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(queue, start=1):
        if not isinstance(item, dict):
            continue
        idea = item.get("idea", {}) if isinstance(item.get("idea"), dict) else {}
        title = idea.get("title", item.get("id", "Kịch bản"))
        pub_tag = "🚀 Tạo & Đăng MXH" if item.get("publish") else "🎬 Tạo & gửi Tele"
        lines.append(f"{idx}. *{title}* — _{pub_tag}_")
        item_id = str(item.get("id") or "")
        if item_id:
            token = uuid.uuid4().hex[:12]
            _queue_run_cache[token] = item_id
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"▶ Chạy trước: {str(title)[:35]}",
                        callback_data=f"queue_run:{token}",
                    )
                ]
            )
    lines.append(
        "\n_Bấm “Chạy trước” để đưa một mục lên đầu hàng đợi. "
        "Worker sẽ xử lý mục đó trước rồi tiếp tục các mục còn lại._"
    )
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=markup,
    )


async def _run_background_ideas_generation(
    bot: Bot,
    chat_id: str | int,
    prompt: str,
    ideas_count: int,
) -> None:
    from core.gemini.idea_generator import generate_ideas
    try:
        ideas = await asyncio.to_thread(
            generate_ideas,
            prompt=prompt,
            scene_count=3,
            duration_seconds=10,
            ideas_count=ideas_count,
        )
        await send_draft_ideas(ideas, bot=bot)
    except Exception as exc:
        LOGGER.exception("Lỗi khi tạo ý tưởng từ Telegram.")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Không tạo được ý tưởng kịch bản: {exc}\nVui lòng thử lại sau.",
            )
        except Exception:
            pass


async def _send_trending_topic_suggestions(message: Any) -> None:
    status_message = await message.reply_text(
        "⏳ Đang lấy tín hiệu Google Trends Việt Nam rồi nhờ Gemini chuyển thành "
        "tình huống hư cấu phù hợp với khỉ và ông chú..."
    )
    try:
        topics = await asyncio.to_thread(suggest_trending_topics, 5)
    except Exception:
        LOGGER.exception("Could not generate trending topic suggestions for Telegram.")
        await status_message.edit_text(
            "⚠️ Chưa lấy/tạo được gợi ý từ Google Trends Việt Nam. Hãy kiểm tra kết nối "
            "Internet, API key/model Gemini và terminal đang chạy bot, sau đó thử lại."
        )
        return

    buttons = []
    for topic in topics:
        short_label = (topic[:38] + "...") if len(topic) > 38 else topic
        token = uuid.uuid4().hex[:8]
        _trending_topics_cache[token] = topic
        buttons.append(
            [
                InlineKeyboardButton(
                    f"💡 {short_label}", callback_data=f"pick_topic:{token}"
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                "🔄 Đổi 5 chủ đề khác", callback_data="menu:suggest_topics"
            )
        ]
    )
    buttons.append([InlineKeyboardButton("❌ Đóng", callback_data="menu:cancel")])
    await status_message.edit_text(
        "🐒 *GỢI Ý TÌNH HUỐNG TỪ GOOGLE TRENDS VIỆT NAM:*\n\n"
        "Mỗi chủ đề có từ khóa, hạng RSS, traffic ước tính và thời điểm lấy dữ liệu. "
        "Đây là tín hiệu tìm kiếm, không đảm bảo video sẽ viral; chủ đề nhạy cảm đã được lọc. "
        "Bấm chọn để Gemini viết kịch bản hư cấu, hoặc bấm *Đổi 5 chủ đề khác*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _send_choice_menu(message: Any) -> None:
    buttons = []
    for label, topic in CHOICE_TOPIC_PRESETS:
        token = uuid.uuid4().hex[:8]
        _trending_topics_cache[token] = topic
        buttons.append(
            InlineKeyboardButton(label, callback_data=f"pick_topic:{token}")
        )

    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "🧩 Đưa tình huống để Gemini phát triển",
                    callback_data="menu:custom_situation",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎲 Gemini nghĩ thêm 5 chủ đề",
                    callback_data="menu:suggest_topics",
                )
            ],
            [InlineKeyboardButton("❌ Đóng", callback_data="menu:cancel")],
        ]
    )
    await message.reply_text(
        "🐒 *Chọn kiểu kịch bản cho bộ đôi khỉ chủ tiệm sửa xe và ông chú (bối cảnh tự do, không nhất thiết ở tiệm):*\n\n"
        "Có hóng drama, hỏi ngu hỏi dại, trend hot, hỏi AI, phỏng vấn, tỏ tình, review quán và nhiều chuyện đời thường. "
        "Chọn một mục, nhập tình huống riêng nếu muốn, rồi chọn số lượng kịch bản Gemini tạo.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


# Persistent bottom keyboard (like other bots). Button text -> menu action.
MENU_BUTTONS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("✍️ Ý tưởng mới", "prompt_theme"), ("💡 Gợi ý chủ đề", "suggest_topics")),
    (("🎬 Dán kịch bản", "paste_script"), ("⚡ Prompt Flow", "direct_prompt")),
    (("📋 Hàng đợi", "view_queue"), ("⚙️ Cấu hình", "flow_settings")),
    (("🎵 Bóc thoại TikTok", "tiktok_transcribe"), ("📜 Log", "view_logs"), ("❌ Hủy", "cancel")),
)
MENU_BUTTON_ACTIONS: dict[str, str] = {
    label: action for row in MENU_BUTTONS for label, action in row
}


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label, _ in row] for row in MENU_BUTTONS],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Gõ chủ đề video hoặc chọn chức năng…",
    )


# Bump when MENU_BUTTONS changes so the new bottom keyboard is pushed once automatically.
MENU_KEYBOARD_VERSION = "5"
MENU_VERSION_FILE = STORAGE_DIR / "telegram_menu_version.txt"


async def ensure_menu_keyboard(bot: Bot, force: bool = False) -> bool:
    """Send the bottom keyboard to the configured chat once per MENU_KEYBOARD_VERSION.

    A reply keyboard only appears after the bot sends it, so without this the user would have to
    type /menu after every menu change. Returns True when a message was sent.
    """
    try:
        sent_version = MENU_VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        sent_version = ""
    if sent_version == MENU_KEYBOARD_VERSION and not force:
        return False
    try:
        await bot.send_message(
            chat_id=get_setting("TELEGRAM_CHAT_ID"),
            text=(
                "🎬 *Menu mới đã sẵn sàng* ở bàn phím bên dưới 👇\n"
                "Gõ chủ đề để Gemini lên kịch bản, hoặc bấm chức năng."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        LOGGER.exception("Không gửi được bàn phím menu Telegram.")
        return False
    try:
        MENU_VERSION_FILE.write_text(MENU_KEYBOARD_VERSION, encoding="utf-8")
    except OSError:
        pass
    LOGGER.info("Đã gửi bàn phím menu Telegram (phiên bản %s).", MENU_KEYBOARD_VERSION)
    return True


async def _run_menu_action(message: Any, action: str) -> None:
    """Shared by the bottom keyboard, the old inline menu buttons and /commands."""
    chat_id = str(message.chat_id)
    _reset_session(chat_id)  # a menu tap always starts fresh (no stale "waiting_*" state)
    transcribe_bot.cancel(chat_id)
    session = _get_session(chat_id)

    if action == "prompt_theme":
        session["state"] = "waiting_theme"
        session.pop("brief_type", None)
        await message.reply_text(
            "✍️ *Gửi chủ đề / ý tưởng video*\n"
            "_Ví dụ: vợ giấu quỹ đen, khỉ bán wifi theo cân, thầy bói vỉa hè…_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if action == "custom_situation":
        session["state"] = "waiting_situation"
        session["brief_type"] = "situation"
        await message.reply_text(
            "🧩 *Gửi tình huống thô* — Gemini giữ mấu chốt gây cười và viết thành kịch bản.\n"
            "_Ví dụ: khỉ bán gà, bán 1 con thì biết giá, khách hỏi 2 con thì không biết._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if action == "paste_script":
        session["state"] = "waiting_script"
        await message.reply_text(
            "🎬 *Dán kịch bản có sẵn*\n"
            "Mỗi cảnh bắt đầu bằng `Scene 1 — Input / First Frame: ...`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if action == "direct_prompt":
        session["state"] = "waiting_direct"
        session["direct_scenes"] = []
        await message.reply_text(
            "⚡ *Gửi prompt Flow trực tiếp* (không qua Gemini), mỗi tin nhắn một cảnh.\n"
            "Có thể gửi kèm `Scene 1 — Input / First Frame: ...` hoặc chỉ gửi nội dung prompt.\n"
            "Sau cảnh cuối, bấm “✅ Xong, chọn cảnh” để chọn các cảnh cần chạy chung.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if action == "view_queue":
        await _send_queue_status(message)
        return
    if action == "suggest_topics":
        await message.reply_text("⏳ Đang nhờ Gemini gợi ý chủ đề…")
        await _send_trending_topic_suggestions(message)
        return
    if action == "tiktok_transcribe":
        await transcribe_bot.start_link_mode(message)
        return
    if action == "view_logs":
        await log_bot.send_menu(message)
        return
    if action in {"flow_settings", "quality"}:
        await flow_settings_bot.send_menu(message)
        return
    if action == "cancel":
        _reset_session(chat_id)
        await message.reply_text("👌 Đã hủy thao tác.", reply_markup=main_menu_keyboard())
        return


async def handle_menu_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    _reset_session(message.chat_id)
    await message.reply_text(
        "🎬 *Video hài Khỉ & Người*\n"
        "Gõ chủ đề để Gemini lên kịch bản, hoặc chọn chức năng ở bàn phím bên dưới 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


async def handle_quality_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    from core.config import get_runtime_pref

    current = "1080" if get_runtime_pref("FLOW_DOWNLOAD_QUALITY", "720").strip().lower().rstrip("p") == "1080" else "720"
    mark = lambda q: "✅ " if q == current else ""  # noqa: E731
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{mark('720')}720p (nhẹ, nhanh)", callback_data="set_quality:720"),
                InlineKeyboardButton(f"{mark('1080')}1080p (nét hơn)", callback_data="set_quality:1080"),
            ]
        ]
    )
    await message.reply_text(
        f"🎞 Chất lượng tải video hiện tại: *{current}p*\nChọn chất lượng tải từ Google Flow:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def handle_queue_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    await _send_queue_status(message)


async def _send_script_draft_preview(
    message: Any,
    idea: dict[str, Any],
    source: str,
    allow_refine: bool = True,
) -> None:
    draft_id = uuid.uuid4().hex[:16]
    append_pending_draft(draft_id, idea, source=source)
    buttons = [
        [
            InlineKeyboardButton(
                "🎬 Tạo video → gửi Tele",
                callback_data=f"prod_tele:{draft_id}",
            ),
            InlineKeyboardButton(
                "🚀 Tạo + Đăng MXH",
                callback_data=f"prod_pub:{draft_id}",
            ),
        ]
    ]
    if allow_refine:
        buttons.append(
            [
                InlineKeyboardButton(
                    "💡 Gemini sửa thoại & twist",
                    callback_data=f"refine_script:{draft_id}",
                ),
                InlineKeyboardButton("❌ Hủy bỏ", callback_data=f"skip:{draft_id}"),
            ]
        )
    else:
        buttons.append(
            [InlineKeyboardButton("❌ Hủy bỏ", callback_data=f"skip:{draft_id}")]
        )
    await message.reply_text(
        _format_idea(
            idea, 1, status_label="📝 ĐÃ NHẬN KỊCH BẢN — Chọn cách tạo:"
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _direct_scene_selection_markup(
    collection_id: str,
    scenes: list[dict[str, Any]],
    selected: set[int],
) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                f"{'✅' if index in selected else '⬜'} Cảnh {index + 1}",
                callback_data=f"direct_toggle:{collection_id}:{index}",
            )
        ]
        for index in range(len(scenes))
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                "✅ Xác nhận các cảnh đã chọn",
                callback_data=f"direct_confirm:{collection_id}",
            ),
            InlineKeyboardButton("❌ Hủy", callback_data=f"direct_cancel:{collection_id}"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def _direct_scene_selection_text(
    scenes: list[dict[str, Any]], selected: set[int]
) -> str:
    lines = [
        f"📋 Đã nhận {len(scenes)} cảnh. Chọn các cảnh sẽ chạy chung trong một video:",
    ]
    for index, scene in enumerate(scenes):
        _, description = _resolve_scene_text(scene)
        lines.append(
            f"{'✅' if index in selected else '⬜'} Cảnh {index + 1}: {description[:100]}"
        )
    return "\n".join(lines)


async def _show_direct_scene_selection(message: Any, session: dict[str, Any]) -> None:
    scenes = session["direct_scenes"]
    selected = session["direct_selected"]
    await message.reply_text(
        _direct_scene_selection_text(scenes, selected),
        reply_markup=_direct_scene_selection_markup(
            session["direct_collection_id"], scenes, selected
        ),
    )


async def _collect_direct_prompt(
    message: Any, text: str, session: dict[str, Any]
) -> None:
    scenes = session.setdefault("direct_scenes", [])
    try:
        if looks_like_direct_prompt(text):
            idea = parse_direct_prompt(text)
        else:
            idea = parse_direct_prompt(
                f"Scene {len(scenes) + 1} — Input / First Frame: {text}"
            )
    except ScriptParseError as exc:
        await message.reply_text(
            f"⚠️ Không đọc được cảnh này:\n{exc}\n\n"
            "Hãy gửi mỗi cảnh trong một tin nhắn, có thể kèm tiêu đề "
            "`Scene 1 — Input / First Frame: ...` hoặc chỉ gửi nội dung prompt.",
        )
        return

    incoming_scenes = idea["scenes"]
    if len(scenes) + len(incoming_scenes) > 20:
        await message.reply_text(
            "⚠️ Tối đa 20 cảnh. Bấm “✅ Xong, chọn cảnh” để tiếp tục."
        )
        return
    first_number = len(scenes) + 1
    for scene in incoming_scenes:
        scene["scene_title"] = f"Scene {len(scenes) + 1}"
        scenes.append(scene)
    session["state"] = "waiting_direct"
    accepted_count = len(incoming_scenes)
    range_label = (
        f"Cảnh {first_number}"
        if accepted_count == 1
        else f"{accepted_count} cảnh"
    )
    await message.reply_text(
        f"✅ Đã nhận {range_label}. Gửi prompt tiếp theo hoặc bấm “✅ Xong, chọn cảnh”.",
        reply_markup=ReplyKeyboardMarkup(
            [
                [KeyboardButton("✅ Xong, chọn cảnh")],
                [KeyboardButton("❌ Hủy")],
            ],
            resize_keyboard=True,
            input_field_placeholder="Dán prompt cảnh tiếp theo…",
        ),
    )


async def handle_script_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    session = _get_session(message.chat_id)
    session["state"] = "waiting_script"
    session["theme"] = ""
    await message.reply_text(
        "🎬 Hãy dán kịch bản có sẵn gồm các Scene được đánh số "
        "(ví dụ: `Scene 1 — ...`). Bot sẽ xử lý mà không gọi Gemini.\n\n"
        "Gõ /cancel nếu muốn hủy.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_direct_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    chat_id = str(message.chat_id)
    session = _get_session(chat_id)
    session["state"] = "waiting_direct"
    session["theme"] = ""
    session["direct_scenes"] = []

    text = re.sub(
        r"^/direct(?:@\w+)?(?:\s+|$)",
        "",
        message.text or "",
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    if text:
        await _collect_direct_prompt(message, text, session)
        return

    await message.reply_text(
        "⚡ Gửi prompt Google Flow từng cảnh, mỗi tin nhắn một cảnh. "
        "Có thể gửi kèm tiêu đề `Scene 1 — Input / First Frame: ...` "
        "hoặc chỉ dán nội dung prompt.\n\n"
        "Sau cảnh cuối, bấm “✅ Xong, chọn cảnh” để chọn các cảnh cần chạy chung. "
        "Bot giữ nguyên prompt và không gọi Gemini. Gõ /cancel để hủy.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    _reset_session(message.chat_id)
    transcribe_bot.cancel(message.chat_id)
    await message.reply_text(
        "👌 Đã hủy thao tác đang chờ. Gõ /menu để mở lại bảng chọn.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_idea_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    args = context.args
    theme = " ".join(args).strip() if args else ""
    if not theme:
        session = _get_session(message.chat_id)
        session["state"] = "waiting_theme"
        await message.reply_text(
            "✍️ Hãy gửi chủ đề bạn muốn tạo (Ví dụ: `/idea nói xấu sếp bị bắt quả tang`) hoặc gõ trực tiếp bên dưới:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    session = _get_session(message.chat_id)
    session["theme"] = theme
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚡ 3 ý tưởng", callback_data="gen_count:3"),
                InlineKeyboardButton("⭐ 5 ý tưởng", callback_data="gen_count:5"),
                InlineKeyboardButton("🔥 10 ý tưởng", callback_data="gen_count:10"),
            ],
            [
                InlineKeyboardButton("❌ Hủy", callback_data="menu:cancel"),
            ],
        ]
    )
    await message.reply_text(
        f"🎯 Chủ đề: *\"{theme}\"*\n\n❓ Bạn muốn tạo bao nhiêu kịch bản?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def handle_choice_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    await _send_choice_menu(message)


async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not message.text:
        return
    chat_id = str(message.chat_id)
    if not _is_authorized_chat(chat_id):
        return

    text = message.text.strip()
    session = _get_session(chat_id)

    if text not in MENU_BUTTON_ACTIONS and await transcribe_bot.handle_text(message, text):
        return

    if session.get("state") == "waiting_direct":
        if text == "✅ Xong, chọn cảnh":
            scenes = session.get("direct_scenes", [])
            if not scenes:
                await message.reply_text(
                    "⚠️ Chưa có cảnh nào. Hãy gửi ít nhất một prompt trước."
                )
                return
            session["state"] = "selecting_direct_scenes"
            session["direct_collection_id"] = uuid.uuid4().hex[:10]
            session["direct_selected"] = set(range(len(scenes)))
            await message.reply_text(
                f"✅ Đã nhận {len(scenes)} cảnh.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await _show_direct_scene_selection(message, session)
            return
        if text == "❌ Hủy":
            _reset_session(chat_id)
            await message.reply_text(
                "👌 Đã hủy thao tác.",
                reply_markup=main_menu_keyboard(),
            )
            return
        await _collect_direct_prompt(message, text, session)
        return

    if text in MENU_BUTTON_ACTIONS:
        await _run_menu_action(message, MENU_BUTTON_ACTIONS[text])
        return

    # 1. Direct prompt pasted (e.g. Scene 1 (6 giây – Nhắc nhở lùi xe)...)
    if looks_like_direct_prompt(text):
        _reset_session(chat_id)
        session = _get_session(chat_id)
        session["state"] = "waiting_direct"
        await _collect_direct_prompt(message, text, session)
        return

    # 2. Direct script pasted (Scene 1 — Input / First Frame...)
    if looks_like_script(text):
        _reset_session(chat_id)
        try:
            idea = parse_script(text, characters=_character_pairs())
        except ScriptParseError as exc:
            await message.reply_text(
                f"⚠️ *Không thể đọc kịch bản:*\n{exc}\n\n"
                "Hãy kiểm tra lại kịch bản có định dạng `Scene 1 — Input / First Frame: ...` và câu thoại trong ngoặc kép.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await _send_script_draft_preview(
            message, idea, source="telegram_script"
        )
        return

    # 2. Waiting for theme
    if session.get("state") in {"waiting_theme", "waiting_situation"}:
        is_situation = session["state"] == "waiting_situation"
        session["theme"] = text
        session["state"] = "waiting_count"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⚡ 3 ý tưởng", callback_data="gen_count:3"
                    ),
                    InlineKeyboardButton(
                        "⭐ 5 ý tưởng", callback_data="gen_count:5"
                    ),
                    InlineKeyboardButton(
                        "🔥 10 ý tưởng", callback_data="gen_count:10"
                    ),
                ],
                [
                    InlineKeyboardButton("❌ Hủy", callback_data="menu:cancel"),
                ],
            ]
        )
        await message.reply_text(
            (
                f"🧩 Tình huống gốc: *\"{text}\"*\n\n"
                "Gemini sẽ giữ mấu chốt rồi phát triển thành các biến thể kịch bản.\n\n"
                "❓ Bạn muốn Gemini tạo bao nhiêu kịch bản?"
                if is_situation
                else f"🎯 Chủ đề: *\"{text}\"*\n\n❓ Bạn muốn Gemini tạo bao nhiêu kịch bản?"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
        return

    # 3. Waiting for script but didn't match format
    if session.get("state") == "waiting_script":
        await message.reply_text(
            "⚠️ Nội dung vừa dán chưa có cú pháp `Scene 1 — ...`.\n\n"
            "Hãy đảm bảo kịch bản bắt đầu bằng `Scene 1 — Input / First Frame: ...` hoặc gõ /cancel để hủy.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # 4. Normal text input -> suggest generating ideas with it
    session["theme"] = text
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚡ Tạo 3 ý tưởng", callback_data="gen_count:3"
                ),
                InlineKeyboardButton(
                    "🔥 Tạo 10 ý tưởng", callback_data="gen_count:10"
                ),
            ],
            [
                InlineKeyboardButton("❌ Bỏ qua", callback_data="menu:cancel"),
            ],
        ]
    )
    await message.reply_text(
        f"💡 Bạn có muốn AI tạo kịch bản theo yêu cầu:\n*\"{text[:200]}\"*?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def handle_flow_settings_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    await flow_settings_bot.send_menu(message)


async def handle_flow_settings_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    if not _is_authorized_chat(query.message.chat_id):
        await query.answer("Bạn không có quyền sử dụng nút này.", show_alert=True)
        return
    await flow_settings_bot.handle_callback(query)


async def handle_transcribe_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    _reset_session(message.chat_id)
    await transcribe_bot.handle_command(message, list(context.args or []))


async def handle_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not _is_authorized_chat(message.chat_id):
        return
    view = (context.args or ["recent"])[0].lower()
    await log_bot.send_menu(message, view if view in log_bot.VIEWS else "recent")


async def handle_transcribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    if not _is_authorized_chat(query.message.chat_id):
        await query.answer("Bạn không có quyền sử dụng nút này.", show_alert=True)
        return
    _reset_session(query.message.chat_id)
    await transcribe_bot.handle_callback(query)


async def handle_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    if not _is_authorized_chat(query.message.chat_id):
        await query.answer("Bạn không có quyền sử dụng nút này.", show_alert=True)
        return
    await log_bot.handle_callback(query)


def register_approval_handler(application: Any) -> None:
    application.add_handler(CommandHandler(transcribe_bot.COMMANDS, handle_transcribe_command))
    application.add_handler(CommandHandler(log_bot.COMMANDS, handle_log_command))
    application.add_handler(
        CallbackQueryHandler(handle_transcribe_callback, pattern=rf"^{transcribe_bot.CALLBACK_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(handle_log_callback, pattern=rf"^{log_bot.CALLBACK_PREFIX}")
    )
    application.add_handler(
        CommandHandler(["start", "menu", "help"], handle_menu_command)
    )
    application.add_handler(
        CommandHandler(["hang_doi", "queue"], handle_queue_command)
    )
    application.add_handler(
        CommandHandler(["new", "dan_kich_ban"], handle_script_command)
    )
    application.add_handler(CommandHandler(["quality", "chat_luong"], handle_quality_command))
    application.add_handler(CommandHandler("direct", handle_direct_command))
    application.add_handler(CommandHandler("cancel", handle_cancel_command))
    application.add_handler(CommandHandler("choice", handle_choice_command))
    application.add_handler(
        CommandHandler(["tao_y_tuong", "idea", "gemini", "genimi"], handle_idea_command)
    )
    application.add_handler(
        CommandHandler(["flow", "cau_hinh", "cauhinh"], handle_flow_settings_command)
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_flow_settings_callback, pattern=rf"^{flow_settings_bot.CALLBACK_PREFIX}"
        )
    )
    application.add_handler(CallbackQueryHandler(handle_approval_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )
