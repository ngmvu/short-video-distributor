"""Telegram /log — read storage/dashboard.log from the chat (secrets redacted).

Callback data: "logview:<view>" with view in VIEWS, or "logview:file".
"""
from __future__ import annotations

import html
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from core.config import STORAGE_DIR

CALLBACK_PREFIX = "logview:"
COMMANDS = ["log", "logs", "xem_log"]
LOG_FILE = STORAGE_DIR / "dashboard.log"
LOG_BACKUP_FILE = STORAGE_DIR / "dashboard.log.1"
READ_BYTES = 512 * 1024
MESSAGE_LIMIT = 3800
FILE_LIMIT_BYTES = 5 * 1024 * 1024

_RECORD_START = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (\w+) ([\w.]+): ")
_NOISE = (
    "HTTP Request:",
    "unauthenticated requests to the HF Hub",
)
_SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD", re.IGNORECASE)
CRAWL_LOGGERS = ("short_video_distributor.thoai_hot", "faster_whisper")


@dataclass
class LogRecord:
    level: str
    logger: str
    lines: list[str] = field(default_factory=list)

    @property
    def header(self) -> str:
        return self.lines[0]

    def summary(self) -> str:
        """Header plus the final exception line, so tracebacks take 2 lines instead of 40."""
        tail = [line for line in self.lines[1:] if line.strip()]
        if not tail:
            return self.header
        last = tail[-1].strip()
        return f"{self.header}\n    ↳ {last}" if last != self.header else self.header


VIEWS: dict[str, tuple[str, str]] = {
    "crawl": ("🎵 Bóc thoại", "Log tải / Whisper khi bóc thoại link TikTok"),
    "errors": ("⚠️ Lỗi & cảnh báo", "Chỉ WARNING / ERROR, traceback rút gọn"),
    "recent": ("📜 Mới nhất", "Các dòng log mới nhất (bỏ dòng HTTP)"),
}


def _decode_escapes(text: str) -> str:
    # Older lines were written through a cp1252 console as "\u0110ang …"; show them as Vietnamese.
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)


def redact(text: str) -> str:
    for name, value in os.environ.items():
        if not _SECRET_NAME.search(name):
            continue
        for part in value.replace(";", ",").split(","):
            secret = part.strip()
            if len(secret) >= 8:
                text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(access_token|bot_token|api[_-]?key|token)(=|%3D)[^&\s\"']+", r"\1\2[REDACTED]", text
    )
    return re.sub(r"\bbot\d{5,}:[A-Za-z0-9_-]{20,}", "bot[REDACTED]", text)


def _read_tail(path: Path, max_bytes: int) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        raw = handle.read().replace(b"\x00", b"")
    text = raw.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def read_log_text(max_bytes: int = READ_BYTES) -> str:
    text = _read_tail(LOG_FILE, max_bytes)
    if len(text) < max_bytes:
        text = _read_tail(LOG_BACKUP_FILE, max_bytes - len(text)) + "\n" + text
    # yt-dlp progress bars use "\r"; keep only the final state of each line.
    lines = [line.rsplit("\r", 1)[-1] for line in text.splitlines()]
    return redact(_decode_escapes("\n".join(lines)))


def parse_records(text: str) -> list[LogRecord]:
    records: list[LogRecord] = []
    for line in text.splitlines():
        match = _RECORD_START.match(line)
        if match:
            records.append(LogRecord(match.group(1), match.group(2), [line]))
        elif records:
            records[-1].lines.append(line)
        elif line.strip():
            records.append(LogRecord("INFO", "", [line]))
    return records


def select_lines(records: list[LogRecord], view: str) -> list[str]:
    if view == "crawl":
        chosen = [r for r in records if r.logger.startswith(CRAWL_LOGGERS)]
        return [r.summary() if r.level in {"WARNING", "ERROR", "CRITICAL"} else r.header for r in chosen]
    if view == "errors":
        return [r.summary() for r in records if r.level in {"WARNING", "ERROR", "CRITICAL"}]
    return [
        r.summary() if r.level in {"ERROR", "CRITICAL"} else r.header
        for r in records
        if not any(noise in r.header for noise in _NOISE)
    ]


def _shorten(line: str) -> str:
    # "2026-11-14 16:47:21,098 INFO short_video_distributor.thoai_hot: msg" -> "16:47:21 INFO msg"
    match = _RECORD_START.match(line)
    if match:
        line = f"{line[11:19]} {match.group(1)} {line[match.end():]}"
    return line if len(line) <= 400 else line[:400] + "…"


def render_view(view: str) -> str:
    title, _help = VIEWS.get(view, VIEWS["recent"])
    records = parse_records(read_log_text())
    lines = [_shorten(line) for line in select_lines(records, view)]
    if not lines:
        body = "(chưa có dòng log nào)"
    else:
        kept: list[str] = []
        size = 0
        for line in reversed(lines):  # newest last, keep as many newest lines as fit
            size += len(html.escape(line, quote=False)) + 1
            if size > MESSAGE_LIMIT - 200:
                break
            kept.append(line)
        body = "\n".join(reversed(kept))
    return f"<b>{title}</b> — <code>storage/dashboard.log</code>\n<pre>{html.escape(body, quote=False)}</pre>"


def keyboard(current: str = "") -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(("• " if key == current else "") + label, callback_data=f"{CALLBACK_PREFIX}{key}")
        for key, (label, _help) in VIEWS.items()
    ]
    return InlineKeyboardMarkup(
        [
            row,
            [
                InlineKeyboardButton("🔄 Làm mới", callback_data=f"{CALLBACK_PREFIX}{current or 'recent'}"),
                InlineKeyboardButton("📎 Tải file log", callback_data=f"{CALLBACK_PREFIX}file"),
            ],
        ]
    )


async def send_menu(message: Any, view: str = "recent") -> None:
    await message.reply_text(render_view(view), parse_mode=ParseMode.HTML, reply_markup=keyboard(view))


async def handle_callback(query: Any) -> None:
    view = str(query.data or "")[len(CALLBACK_PREFIX):]
    if view == "file":
        await query.answer("Đang gửi file log…")
        data = read_log_text(FILE_LIMIT_BYTES).encode("utf-8")
        await query.message.reply_document(
            document=io.BytesIO(data),
            filename="dashboard.log.txt",
            caption="📎 Log (đã ẩn token/key).",
        )
        return
    if view not in VIEWS:
        await query.answer()
        return
    await query.answer()
    try:
        await query.edit_message_text(
            render_view(view), parse_mode=ParseMode.HTML, reply_markup=keyboard(view)
        )
    except BadRequest as exc:  # "message is not modified" when nothing new was logged
        if "not modified" not in str(exc).lower():
            raise
