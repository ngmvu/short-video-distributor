"""Telegram "📥 Import video" — bring a video into storage/downloads, then act on it.

Send a video to the bot (as video or file, ≤20 MB — the Bot API download limit), or a path to a
video on this machine. The bot saves it into storage/downloads (so it also shows up on the Web UI)
and offers buttons:
  🧽 Xóa logo ✦     — remove the Gemini sparkle (result gets the same buttons);
  ✨ Caption & đăng — Gemini writes title/caption/hashtags per platform → preview, toggle
                      TikTok / Facebook / YouTube → 🚀 Đăng ngay (same uploader as the Web UI).

Videos sent outside any mode are imported too. Button state lives in storage/telegram_imports.json
so buttons keep working after a bot restart.

Callback data: "imp:<action>:<token>"
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from core.config import DOWNLOADS_DIR, STORAGE_DIR
from core.storage import read_json, save_publish_report, save_video_social_metadata, write_json_atomic
from core.tele import watermark_bot
from core.tele.notifier import send_video_file

LOGGER = logging.getLogger("short_video_distributor.import_bot")
COMMANDS = ["import", "nhap_video"]
CALLBACK_PREFIX = "imp:"
STATE_FILE = STORAGE_DIR / "telegram_imports.json"
MAX_ENTRIES = 60
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
PLATFORMS = (("tiktok", "🎵 TikTok"), ("facebook", "📘 Facebook"), ("youtube", "▶️ YouTube"))
_waiting: set[str] = set()
_busy: set[str] = set()  # tokens with a caption/publish job running (double-tap guard)
_background_tasks: set[asyncio.Task[None]] = set()

PROMPT = (
    "📥 <b>Import video</b>\n"
    "Gửi video vào đây (dạng video hoặc file, tối đa 50 MB),\n"
    "hoặc dán đường dẫn file trên máy (vd: <code>E:\\Videos\\clip.mp4</code>).\n"
    "Video được lưu vào <code>storage/downloads</code> (có luôn trên Web UI), "
    "sau đó chọn: 🧽 xóa logo, ✨ tạo caption & đăng MXH."
)


# ---------------------------------------------------------------- state

def _load() -> dict[str, dict[str, Any]]:
    data = read_json(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def _save(state: dict[str, dict[str, Any]]) -> None:
    if len(state) > MAX_ENTRIES:
        newest = sorted(state.items(), key=lambda kv: kv[1].get("created", 0))[-MAX_ENTRIES:]
        state = dict(newest)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(STATE_FILE, state)


def _get(token: str) -> dict[str, Any] | None:
    entry = _load().get(token)
    return entry if isinstance(entry, dict) else None


def _lock() -> FileLock:
    # The render worker (scene videos) and the bot poller both write this file.
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return FileLock(str(STATE_FILE) + ".lock", timeout=30)


def _update(token: str, **fields: Any) -> dict[str, Any] | None:
    with _lock():
        state = _load()
        if token not in state:
            return None
        state[token].update(fields)
        _save(state)
        return state[token]


def register(video: Path) -> str:
    """Remember a video in storage/downloads and return the token its buttons carry."""
    token = secrets.token_hex(5)
    with _lock():
        state = _load()
        state[token] = {
            "video": video.name,
            "created": time.time(),
            "platforms": [key for key, _ in PLATFORMS],
            "social_posts": None,
            "title": video.stem,
            "published": False,
        }
        _save(state)
    return token


def _video_path(entry: dict[str, Any]) -> Path | None:
    name = str(entry.get("video") or "")
    if not name or Path(name).name != name:
        return None
    path = DOWNLOADS_DIR / name
    return path if path.is_file() else None


# ---------------------------------------------------------------- import

def safe_video_name(original: str) -> str:
    """Free file name in storage/downloads, keeping the original stem when possible."""
    raw = Path(original or "")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", raw.stem).strip(" ._")[:90]
    if not stem:
        stem = "telegram_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{stem}.mp4"
    if (DOWNLOADS_DIR / name).exists():
        name = f"{stem}_{int(time.time())}.mp4"
    return name


def resolve_source_path(text: str) -> Path | None:
    """A pasted path (or a file name already in storage/downloads) → existing video file."""
    raw = text.strip().strip('"').strip("'").strip()
    if not raw or "\n" in raw or not raw.lower().endswith(VIDEO_EXTENSIONS):
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = DOWNLOADS_DIR / raw
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_file() and resolved.stat().st_size > 0 else None


def import_local_file(source: Path) -> Path:
    """Copy a video into storage/downloads (no copy if it is already there)."""
    if source.parent == DOWNLOADS_DIR.resolve():
        return source
    destination = DOWNLOADS_DIR / safe_video_name(source.name)
    shutil.copy2(source, destination)
    return destination


def _attachment(message: Any) -> Any | None:
    if getattr(message, "video", None):
        return message.video
    document = getattr(message, "document", None)
    if document and str(getattr(document, "mime_type", "") or "").startswith("video/"):
        return document
    return None


# ---------------------------------------------------------------- views

def _logo_buttons(token: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("🧽 Xóa logo ✦", callback_data=f"{CALLBACK_PREFIX}logo:{token}"),
        InlineKeyboardButton("🐒 Che logo", callback_data=f"{CALLBACK_PREFIX}stk:{token}"),
    ]


def actions_markup(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            _logo_buttons(token),
            [InlineKeyboardButton("✨ Caption & đăng", callback_data=f"{CALLBACK_PREFIX}cap:{token}")],
        ]
    )


def flow_video_markup(video: str | Path, with_publish: bool = False) -> InlineKeyboardMarkup | None:
    """Buttons for a Flow scene / full video the worker sends to Telegram (None if not in downloads)."""
    video = Path(video)
    if not video.is_file() or video.resolve().parent != DOWNLOADS_DIR.resolve():
        return None
    token = register(video)
    if with_publish:
        return actions_markup(token)
    return InlineKeyboardMarkup([_logo_buttons(token)])


def _imported_text(video: Path) -> str:
    size_mb = video.stat().st_size / 1024 / 1024
    return (
        f"📥 Đã import <code>{html.escape(video.name)}</code> ({size_mb:.1f} MB) vào "
        "<code>storage/downloads</code> — có trên Web UI.\nChọn việc tiếp theo:"
    )


def _cut(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def preview_text(entry: dict[str, Any]) -> str:
    posts = entry.get("social_posts") or {}
    lines = [f"✨ <b>{html.escape(_cut(entry.get('title', ''), 150))}</b>",
             f"🎞 <code>{html.escape(str(entry.get('video', '')))}</code>", ""]
    for key, label in PLATFORMS:
        post = posts.get(key) or {}
        mark = "✅" if key in entry.get("platforms", []) else "⬜"
        lines.append(f"{mark} <b>{label}</b>: {html.escape(_cut(post.get('title', ''), 150))}")
        if post.get("caption"):
            lines.append(html.escape(_cut(post["caption"], 500)))
        if post.get("hashtags"):
            lines.append(f"<i>{html.escape(_cut(post['hashtags'], 200))}</i>")
        lines.append("")
    if entry.get("published"):
        lines.append("ℹ️ Video này đã được đăng từ Telegram rồi.")
    lines.append("Bấm nền tảng để bật/tắt, rồi 🚀 Đăng ngay. Sửa caption chi tiết trên Web UI.")
    return "\n".join(lines)


def preview_markup(token: str, entry: dict[str, Any]) -> InlineKeyboardMarkup:
    chosen = entry.get("platforms", [])
    toggles = [
        InlineKeyboardButton(
            f"{'✅' if key in chosen else '⬜'} {label.split(' ', 1)[1]}",
            callback_data=f"{CALLBACK_PREFIX}t_{key}:{token}",
        )
        for key, label in PLATFORMS
    ]
    return InlineKeyboardMarkup(
        [
            toggles,
            [
                InlineKeyboardButton("🔄 Viết lại caption", callback_data=f"{CALLBACK_PREFIX}cap:{token}"),
                InlineKeyboardButton("🧽 Xóa logo ✦", callback_data=f"{CALLBACK_PREFIX}logo:{token}"),
            ],
            [
                InlineKeyboardButton("🚀 Đăng ngay", callback_data=f"{CALLBACK_PREFIX}pub:{token}"),
                InlineKeyboardButton("✖️ Đóng", callback_data=f"{CALLBACK_PREFIX}close:{token}"),
            ],
        ]
    )


# ---------------------------------------------------------------- entry points

def is_waiting(chat_id: str | int) -> bool:
    return str(chat_id) in _waiting


def cancel(chat_id: str | int) -> None:
    _waiting.discard(str(chat_id))


async def start_mode(message: Any) -> None:
    _waiting.add(str(message.chat_id))
    await message.reply_text(PROMPT, parse_mode=ParseMode.HTML)


async def handle_command(message: Any, args: list[str]) -> None:
    """/import [đường dẫn] — with a path: import now; else wait for a video."""
    if args:
        await _import_path(message, " ".join(args))
        return
    await start_mode(message)


async def handle_text(message: Any, text: str) -> bool:
    """While waiting: a pasted path. Returns True when the message was consumed."""
    if not is_waiting(message.chat_id):
        return False
    await _import_path(message, text)
    return True


async def _import_path(message: Any, text: str) -> None:
    source = resolve_source_path(text)
    if source is None:
        await message.reply_text(
            "⚠️ Không thấy file video ở đường dẫn đó (.mp4, .mov, .webm, .mkv, .m4v).",
        )
        return
    try:
        video = await asyncio.to_thread(import_local_file, source)
    except OSError as exc:
        await message.reply_text(f"❌ Không chép được video: {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
        return
    await _announce(message, video)


async def handle_video(message: Any) -> bool:
    """Import a video sent to the bot. Returns True when it was a video."""
    attachment = _attachment(message)
    if attachment is None:
        return False
    size = int(getattr(attachment, "file_size", 0) or 0)
    if size > watermark_bot.MAX_DOWNLOAD_BYTES:
        await message.reply_text(
            f"⚠️ Video {size / 1024 / 1024:.0f} MB vượt giới hạn 50 MB bot được tải về. "
            "Hãy dán đường dẫn file trên máy (sau /import) thay vì gửi video."
        )
        return True
    status = await message.reply_text("⏳ Đang tải video về…")
    video = DOWNLOADS_DIR / safe_video_name(str(getattr(attachment, "file_name", "") or ""))
    try:
        tg_file = await attachment.get_file()
        await tg_file.download_to_drive(custom_path=video)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Tải video từ Telegram thất bại.")
        video.unlink(missing_ok=True)
        await _edit(status, f"❌ Không tải được video: {html.escape(str(exc)[:300])}")
        return True
    token = register(video)
    await _edit(status, _imported_text(video), actions_markup(token))
    return True


async def _announce(message: Any, video: Path) -> None:
    """Imported from a path on this machine: send the video itself, with the action buttons."""
    token = register(video)
    if video.stat().st_size <= watermark_bot.MAX_SEND_BYTES:
        try:
            await send_video_file(video, _imported_text(video), message.get_bot(), message.chat_id,
                                  actions_markup(token))
            return
        except Exception:  # noqa: BLE001
            LOGGER.exception("Không gửi được video vừa import lên Telegram.")
    await message.reply_text(_imported_text(video), parse_mode=ParseMode.HTML, reply_markup=actions_markup(token))


async def _edit(message: Any, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                disable_web_page_preview=True)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            LOGGER.warning("Không sửa được tin nhắn import: %s", exc)


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------- callbacks

async def handle_callback(query: Any) -> None:
    try:
        action, token = str(query.data or "")[len(CALLBACK_PREFIX):].split(":", 1)
    except ValueError:
        await query.answer()
        return
    entry = _get(token)
    if entry is None or (action != "close" and _video_path(entry) is None):
        await query.answer("Video này không còn trong storage/downloads, hãy import lại.", show_alert=True)
        return
    if action in {"cap", "pub"} and token in _busy:
        await query.answer("⏳ Đang xử lý, chờ chút…")
        return

    if action == "close":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        return
    if action in {"logo", "stk"}:
        sticker = action == "stk"
        await query.answer("🐒 Đang che logo bằng nhãn dán…" if sticker else "🧽 Đang xóa logo…")
        watermark_bot.start_job(
            query.message, local_path=_video_path(entry), markup_for=_markup_for_cleaned,
            mode="sticker" if sticker else "remove",
        )
        return
    if action.startswith("t_"):
        key = action[2:]
        chosen = list(entry.get("platforms", []))
        chosen = [p for p in chosen if p != key] if key in chosen else chosen + [key]
        entry = _update(token, platforms=[p for p, _ in PLATFORMS if p in chosen]) or entry
        await query.answer()
        await _edit(query.message, preview_text(entry), preview_markup(token, entry))
        return
    if action == "cap":
        await query.answer("✨ Gemini đang viết caption…")
        _busy.add(token)
        _spawn(_generate_captions(query.message, token))
        return
    if action == "pub":
        if not entry.get("social_posts"):
            await query.answer("Bấm ✨ Caption & đăng trước để có caption.", show_alert=True)
            return
        if not entry.get("platforms"):
            await query.answer("Chọn ít nhất 1 nền tảng.", show_alert=True)
            return
        await query.answer("🚀 Đang đăng…")
        _busy.add(token)
        _spawn(_publish(query.message, token))
        return
    await query.answer()


def _markup_for_cleaned(output: Path) -> InlineKeyboardMarkup | None:
    """After logo removal: the -nologo copy gets its own import buttons (if in downloads)."""
    if output.parent.resolve() != DOWNLOADS_DIR.resolve():
        return None
    return actions_markup(register(output))


async def _generate_captions(message: Any, token: str) -> None:
    from core.gemini.idea_generator import generate_social_posts_for_video

    try:
        entry = _get(token) or {}
        video = _video_path(entry)
        status = await message.reply_text("⏳ Gemini đang xem video và viết caption cho 3 nền tảng…")
        try:
            generated = await asyncio.to_thread(
                generate_social_posts_for_video, video, video_title=video.stem if video else ""
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Tạo caption bằng Gemini thất bại.")
            await _edit(status, f"❌ Gemini lỗi: {html.escape(str(exc)[:400])}", actions_markup(token))
            return
        raw_title = generated.get("general_title")
        title = (raw_title.get("title") if isinstance(raw_title, dict) else raw_title) or entry.get("title", "")
        posts = {key: generated[key] for key, _ in PLATFORMS if isinstance(generated.get(key), dict)}
        entry = _update(token, social_posts=posts, title=str(title).strip()) or entry
        await asyncio.to_thread(save_video_social_metadata, entry["video"], posts, entry["title"])
        await _edit(status, preview_text(entry), preview_markup(token, entry))
    finally:
        _busy.discard(token)


def build_platform_configs(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Same shape the Web UI's publish endpoint passes to publish_to_social."""
    from core.social_uploader import build_youtube_shorts_title, parse_hashtags

    posts = entry.get("social_posts") or {}
    chosen = set(entry.get("platforms", []))
    general = str(entry.get("title") or Path(str(entry.get("video", ""))).stem)[:120]
    configs: dict[str, dict[str, Any]] = {}
    for key, _ in PLATFORMS:
        post = posts.get(key) or {}
        title = str(post.get("title") or "").strip()
        hashtags = str(post.get("hashtags") or "")
        if key == "youtube":
            title = build_youtube_shorts_title(title or general, hashtags, max_length=100)
        elif key == "tiktok":
            title = title or general
        configs[key] = {
            "enabled": key in chosen,
            "title": title,
            "caption": str(post.get("caption") or "").strip(),
            "hashtags": parse_hashtags(hashtags),
        }
    return configs


def result_text(entry: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> str:
    lines = [f"🚀 <b>Kết quả đăng</b> <code>{html.escape(str(entry.get('video', '')))}</code>"]
    for key, label in PLATFORMS:
        if key not in entry.get("platforms", []):
            continue
        status = statuses.get(key) or {}
        if status.get("success"):
            url = status.get("url") or status.get("permalink") or ""
            lines.append(f"✅ {label}" + (f": {html.escape(str(url))}" if url else ""))
        else:
            reason = status.get("error") or status.get("message") or "không rõ lỗi"
            lines.append(f"❌ {label}: {html.escape(_cut(str(reason), 300))}")
    return "\n".join(lines)


async def _publish(message: Any, token: str) -> None:
    from core.social_uploader import publish_to_social

    try:
        entry = _get(token) or {}
        video = _video_path(entry)
        if video is None:
            await message.reply_text("❌ Không còn file video để đăng.")
            return
        configs = build_platform_configs(entry)
        status = await message.reply_text(
            "⏳ Đang đăng lên " + ", ".join(label for key, label in PLATFORMS if configs[key]["enabled"]) + "…"
        )
        try:
            statuses = await asyncio.to_thread(
                publish_to_social, str(video), title=entry.get("title", ""), description="", tags=[],
                platform_configs=configs,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Đăng video từ Telegram thất bại.")
            await _edit(status, f"❌ Đăng thất bại: {html.escape(str(exc)[:400])}")
            return
        _update(token, published=True)
        await asyncio.to_thread(
            save_publish_report,
            {
                "id": f"pub_{int(time.time())}_{secrets.token_hex(3)}",
                "title": entry.get("title", ""),
                "video_path": str(video),
                "video_name": video.name,
                "published_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "telegram",
                "platform_configs": {
                    key: {**cfg, "hashtags": [f"#{t}" for t in cfg["hashtags"]]}
                    for key, cfg in configs.items()
                },
                "statuses": statuses,
            },
        )
        await _edit(status, result_text(entry, statuses))
    finally:
        _busy.discard(token)
