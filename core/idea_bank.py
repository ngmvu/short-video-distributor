"""Idea bank: where the daily schedule (/lich) gets its topics from.

Sources, in the order /lich uses them:
  1. "user"  — ideas / punchlines you send with /y on Telegram (best quality, always first);
  2. "link"  — a hot TikTok/YouTube video you paste: its speech is transcribed and Gemini keeps
               only the *situation* (never the original lines), so the script is our own;
  3. "trend" — one Google Trends VN topic that fits everyday life, added when the bank is empty;
  4. nothing left → the weekday theme (Mon work, Tue money, Wed proverbs…) + upcoming holidays.

A topic typed in /lich → 🏷 Chủ đề still overrides all of this.

State lives in storage/idea_bank.json:
  [{"id", "text", "source", "created_at", "used_at", "used_for", "meta": {...}}, ...]
"""
from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from filelock import FileLock

from core.config import STORAGE_DIR
from core.storage import read_json, write_json_atomic

LOGGER = logging.getLogger("short_video_distributor.idea_bank")
BANK_FILE = STORAGE_DIR / "idea_bank.json"
SOURCES = ("user", "link", "trend")
SOURCE_LABELS = {"user": "✍️ Bạn gửi", "link": "🔗 Từ link", "trend": "📈 Trend"}
MAX_TEXT = 600
KEEP_USED = 200  # used entries kept for duplicate checks

WEEKDAY_THEMES = (
    "Đi làm: sếp, deadline, họp hành, đồng nghiệp, xin tăng lương",
    "Tiền bạc: giá cả, lương, vay mượn, tiết kiệm, giá vàng, giá xăng",
    "Bẻ nghĩa một câu tục ngữ / ca dao quen thuộc: @khi hiểu theo nghĩa đen rồi chốt bằng "
    "một lý lẽ ngược đời mà ngẫm lại đúng",
    "Công nghệ: AI, điện thoại, mạng xã hội, app ngân hàng, livestream bán hàng",
    "Vợ chồng: quỹ đen, nhậu nhẹt, lương về đưa vợ, đi chơi cuối tuần",
    "Đời sống thường ngày: shipper, mua online nhận hàng khác ảnh, hàng xóm, đi chợ, kẹt xe",
    "Gia đình, họ hàng: con cái học hành, bị hỏi bao giờ cưới, so sánh con nhà người ta",
)
WEEKDAY_NAMES = ("Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật")

# Lunar New Year (mùng 1 Tết) — the Tết season starts ~3 weeks before.
TET_DATES = (date(2027, 2, 6), date(2028, 1, 26), date(2029, 2, 13), date(2030, 2, 3))
# (month, day, hint) — mentioned from 4 days before until the day itself.
HOLIDAYS = (
    (1, 1, "Tết Dương lịch: đếm ngược, mục tiêu năm mới không bao giờ làm"),
    (2, 14, "Valentine: quà tặng, độc thân, ví tiền"),
    (3, 8, "8/3: tặng quà cho vợ, mẹ, đồng nghiệp nữ"),
    (4, 1, "Cá tháng Tư: nói dối, bị lừa"),
    (4, 30, "nghỉ lễ 30/4–1/5: đi du lịch, kẹt xe, giá phòng tăng"),
    (6, 1, "1/6: trẻ con, quà, ông chú cũng đòi làm trẻ con"),
    (9, 2, "nghỉ lễ 2/9: đi chơi, kẹt xe"),
    (9, 5, "khai giảng: sắm đồ cho con, học phí"),
    (10, 20, "20/10: quà cho vợ, hoa, ví mỏng"),
    (11, 20, "20/11: quà thầy cô, nhớ thời đi học"),
    (12, 24, "Noel: ông già Noel, quà, đi chơi đêm"),
)


def _lock() -> FileLock:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return FileLock(str(BANK_FILE) + ".lock", timeout=30)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(text: str) -> str:
    """Lowercase, no accents, no punctuation — for duplicate checks."""
    stripped = unicodedata.normalize("NFD", str(text or "").casefold().replace("đ", "d"))
    stripped = "".join(ch for ch in stripped if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", stripped).strip()


def load() -> list[dict[str, Any]]:
    data = read_json(BANK_FILE, [])
    return [e for e in data if isinstance(e, dict) and e.get("id") and e.get("text")] if isinstance(data, list) else []


def _save(entries: list[dict[str, Any]]) -> None:
    used = [e for e in entries if e.get("used_at")]
    if len(used) > KEEP_USED:
        drop = {e["id"] for e in sorted(used, key=lambda e: e["used_at"])[: len(used) - KEEP_USED]}
        entries = [e for e in entries if e["id"] not in drop]
    write_json_atomic(BANK_FILE, entries)


def unused(entries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Unused ideas in the order /lich takes them: user → link → trend, oldest first."""
    entries = load() if entries is None else entries
    rank = {source: i for i, source in enumerate(SOURCES)}
    return sorted(
        (e for e in entries if not e.get("used_at")),
        key=lambda e: (rank.get(e.get("source"), len(SOURCES)), e.get("created_at", "")),
    )


def add(text: str, source: str = "user", meta: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    """Add an idea. Returns (entry, created); a duplicate returns the existing entry, False."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_TEXT]
    if not text:
        raise ValueError("Ý tưởng trống.")
    if source not in SOURCES:
        raise ValueError(f"Nguồn không hợp lệ: {source}")
    key = _norm(text)
    url = (meta or {}).get("url")
    with _lock():
        entries = load()
        for entry in entries:
            if _norm(entry["text"]) == key or (url and entry.get("meta", {}).get("url") == url):
                return entry, False
        entry = {
            "id": uuid.uuid4().hex[:10],
            "text": text,
            "source": source,
            "created_at": _now(),
            "used_at": None,
            "used_for": None,
            "meta": dict(meta or {}),
        }
        entries.append(entry)
        _save(entries)
        return entry, True


def remove(entry_id: str) -> bool:
    with _lock():
        entries = load()
        kept = [e for e in entries if e["id"] != entry_id]
        if len(kept) == len(entries):
            return False
        _save(kept)
        return True


def mark_used(entry_id: str, used_for: str = "") -> None:
    with _lock():
        entries = load()
        for entry in entries:
            if entry["id"] == entry_id:
                entry["used_at"] = _now()
                entry["used_for"] = used_for or None
        _save(entries)


def stats(entries: list[dict[str, Any]] | None = None) -> dict[str, int]:
    pending = unused(entries)
    counts = {source: 0 for source in SOURCES}
    for entry in pending:
        counts[entry.get("source", "user")] = counts.get(entry.get("source", "user"), 0) + 1
    counts["total"] = len(pending)
    return counts


# ------------------------------------------------------------------ fallbacks

def seasonal_hint(day: date) -> str:
    """Holiday / Tết hint for a post date, or ""."""
    for tet in TET_DATES:
        if tet - timedelta(days=21) <= day <= tet + timedelta(days=6):
            return (
                "không khí Tết: thưởng Tết, sắm Tết, dọn nhà, về quê, lì xì, "
                "bị họ hàng hỏi lương / bao giờ cưới"
            )
    for month, dom, hint in HOLIDAYS:
        try:
            holiday = date(day.year, month, dom)
        except ValueError:
            continue
        if holiday - timedelta(days=4) <= day <= holiday:
            return hint
    # Black Friday: the Friday after the 4th Thursday of November.
    if day.month == 11:
        first = date(day.year, 11, 1)
        thursday = first + timedelta(days=(3 - first.weekday()) % 7 + 21)
        black_friday = thursday + timedelta(days=1)
        if black_friday - timedelta(days=4) <= day <= black_friday:
            return "Black Friday: săn sale, giỏ hàng đầy, ví trống"
    return ""


def weekday_topic(when: datetime) -> str:
    topic = (
        f"{WEEKDAY_NAMES[when.weekday()]} — {WEEKDAY_THEMES[when.weekday()]}. "
        "Ưu tiên ghép với một trend mạng xã hội đang hot hợp chủ đề này (SKILL mục 8a)"
    )
    hint = seasonal_hint(when.date())
    return f"{topic}. Nếu hợp thì lồng thêm dịp đang tới: {hint}" if hint else topic


def trend_text(title: str) -> str:
    return (
        f"Bắt trend đang hot ở Việt Nam: “{title}” — chỉ mượn chủ đề để chế tình huống đời thường, "
        "hoàn toàn hư cấu, không nhắc tên hay hình ảnh người thật."
    )


def refill_from_trends(limit: int = 1) -> int:
    """Add up to `limit` Google Trends topics that fit the channel and are not in the bank yet."""
    from core.trend_radar import TrendRadarError, get_trend_signals

    try:
        signals = get_trend_signals(limit=30)
    except (TrendRadarError, ValueError, OSError) as exc:
        LOGGER.warning("Không lấy được Google Trends cho kho ý tưởng: %s", exc)
        return 0
    added = 0
    for signal in signals:
        if added >= limit:
            break
        if signal.channel_fit_score <= 0:
            continue
        _, created = add(trend_text(signal.title), "trend", {"trend": signal.title})
        added += int(created)
    return added


def choose_topic(publish_at: datetime, config_topic: str = "") -> dict[str, Any]:
    """The topic for one /lich slot: {"topic", "source", "label", "entry_id"}.

    Pure read (plus a trend refill when the bank is empty) — call mark_used(entry_id)
    once the ideas were actually generated so a Gemini failure does not burn the idea.
    """
    config_topic = (config_topic or "").strip()
    if config_topic:
        return {"topic": config_topic, "source": "config", "label": "🏷 Chủ đề cố định", "entry_id": None}
    pending = unused()
    if not pending:
        refill_from_trends(1)
        pending = unused()
    if pending:
        entry = pending[0]
        return {
            "topic": entry["text"],
            "source": entry["source"],
            "label": f"🧠 Kho ý tưởng · {SOURCE_LABELS.get(entry['source'], entry['source'])}",
            "entry_id": entry["id"],
        }
    return {
        "topic": weekday_topic(publish_at),
        "source": "weekday",
        "label": f"📅 Chủ đề {WEEKDAY_NAMES[publish_at.weekday()]}",
        "entry_id": None,
    }
