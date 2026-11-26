"""Daily auto-publish schedule: Gemini idea (SKILL v16.1) → Telegram approval → Flow → YouTube.

Configured from Telegram (/lich). For each daily post slot (e.g. 19:00):
  slot − lead_hours  → Gemini writes ideas, sent to Telegram with "🗓 Duyệt → đăng 19:00";
  approve            → the idea joins the Flow queue carrying {"schedule": {"publish_at": ...}};
  video rendered     → logo ✦ removed → subscribe outro appended (core.outro) → uploaded to YouTube, private with publishAt = the slot
                       (public right away when the slot already passed) → link sent to Telegram.

Not approved within auto_approve_minutes (default 60) → the idea Gemini scored highest
(viral_score) is approved automatically, skipping ideas the user pressed ❌ on.

State lives in storage/publish_schedule.json:
  {"enabled", "slots": ["11:30", "19:00"], "lead_hours", "ideas_per_slot", "topic",
   "remove_logo", "append_outro", "auto_approve_minutes",
   "generated": {"2026-11-21 19:00": "<iso time ideas were generated>"},
   "batches": {"2026-11-21 19:00": {"sent_at", "publish_at", "draft_ids", "approved"}}}
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from core.config import STORAGE_DIR
from core.storage import read_json, write_json_atomic

LOGGER = logging.getLogger("short_video_distributor.schedule")
CONFIG_FILE = STORAGE_DIR / "publish_schedule.json"
LEAD_CHOICES = (1, 2, 3, 4, 6, 8)
IDEAS_CHOICES = (1, 2, 3)
AUTO_APPROVE_CHOICES = (60, 120, 0, 30)  # minutes; 0 = never auto-approve
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "slots": [],
    "lead_hours": 3,
    "ideas_per_slot": 2,
    "topic": "",
    "remove_logo": True,  # legacy on/off; logo_mode wins when present
    "logo_mode": "",  # "remove" (xóa ✦) | "sticker" (che bằng nhãn dán) | "none" (giữ nguyên)
    "append_outro": True,
    "auto_approve_minutes": 60,
    "generated": {},
    "batches": {},
}
LOGO_MODES = ("remove", "sticker", "none")
LOGO_MODE_LABELS = {"remove": "Xóa", "sticker": "Nhãn dán", "none": "Giữ nguyên"}


def logo_mode(settings: dict[str, Any]) -> str:
    """logo_mode of a config / schedule block, falling back to the old remove_logo flag."""
    mode = str(settings.get("logo_mode") or "").strip()
    if mode in LOGO_MODES:
        return mode
    return "remove" if settings.get("remove_logo", True) else "none"
_SLOT_RE = re.compile(r"^\s*([01]?\d|2[0-3])\s*(?:[:hH.]\s*([0-5]\d)?)?\s*$")


def _lock() -> FileLock:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return FileLock(str(CONFIG_FILE) + ".lock", timeout=30)


def load_config() -> dict[str, Any]:
    data = read_json(CONFIG_FILE, {})
    config = {**DEFAULTS, **(data if isinstance(data, dict) else {})}
    config["slots"] = sorted({s for s in config.get("slots", []) if parse_slot(s)})
    for key in ("generated", "batches"):
        if not isinstance(config.get(key), dict):
            config[key] = {}
    return config


def update_config(**changes: Any) -> dict[str, Any]:
    with _lock():
        config = load_config()
        config.update(changes)
        config["slots"] = sorted({s for s in config.get("slots", []) if parse_slot(s)})
        write_json_atomic(CONFIG_FILE, config)
        return config


def parse_slot(text: str) -> str | None:
    """'19:00' / '7h30' / '19' / '7.5' → 'HH:MM' (None when invalid)."""
    match = _SLOT_RE.match(str(text or ""))
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}"


def parse_slots(text: str) -> list[str]:
    """Every valid time in a message like '11:30, 19h 21:15'."""
    return [slot for part in re.split(r"[,\s;]+", text or "") if (slot := parse_slot(part))]


def _slot_key(publish_at: datetime) -> str:
    return publish_at.strftime("%Y-%m-%d %H:%M")


def upcoming_posts(config: dict[str, Any], now: datetime | None = None) -> list[tuple[datetime, datetime]]:
    """(publish_at, generate_at) for every slot's next occurrence, soonest first (local time)."""
    now = now or datetime.now()
    lead = timedelta(hours=float(config.get("lead_hours") or DEFAULTS["lead_hours"]))
    result = []
    for slot in config.get("slots", []):
        hour, minute = map(int, slot.split(":"))
        publish_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if publish_at <= now:
            publish_at += timedelta(days=1)
        result.append((publish_at, publish_at - lead))
    return sorted(result)


def due_posts(config: dict[str, Any], now: datetime | None = None) -> list[datetime]:
    """Posts whose ideas should be generated now and haven't been yet."""
    if not config.get("enabled"):
        return []
    now = now or datetime.now()
    generated = config.get("generated", {})
    return [
        publish_at
        for publish_at, generate_at in upcoming_posts(config, now)
        if generate_at <= now and _slot_key(publish_at) not in generated
    ]


def mark_generated(publish_at: datetime) -> None:
    with _lock():
        config = load_config()
        generated = dict(config.get("generated", {}))
        generated[_slot_key(publish_at)] = datetime.now().isoformat(timespec="seconds")
        # keep two weeks of history
        cutoff = _slot_key(datetime.now() - timedelta(days=14))
        config["generated"] = {k: v for k, v in generated.items() if k >= cutoff}
        write_json_atomic(CONFIG_FILE, config)


def record_batch(publish_at: datetime, draft_ids: list[str], topic: dict[str, Any] | None = None) -> None:
    """Remember which drafts were offered for a slot (the auto-approve clock starts at the first).

    `topic` (from core.idea_bank.choose_topic) is kept so "🔄 Thêm ý khác" stays on the same topic.
    """
    with _lock():
        config = load_config()
        batches = dict(config["batches"])
        key = _slot_key(publish_at)
        batch = dict(batches.get(key) or {})
        batch.setdefault("sent_at", datetime.now().isoformat(timespec="seconds"))
        if topic and not batch.get("topic"):
            batch["topic"] = {k: topic.get(k) for k in ("topic", "source", "label", "entry_id")}
        batch["publish_at"] = publish_at.astimezone().isoformat(timespec="seconds")
        batch["draft_ids"] = list(dict.fromkeys([*batch.get("draft_ids", []), *draft_ids]))
        batch.setdefault("approved", None)
        batches[key] = batch
        cutoff = _slot_key(datetime.now() - timedelta(days=14))
        config["batches"] = {k: v for k, v in batches.items() if k >= cutoff}
        write_json_atomic(CONFIG_FILE, config)


def batch_topic(publish_at: datetime) -> dict[str, Any] | None:
    """The topic already used for this slot's ideas, if any."""
    batch = load_config()["batches"].get(_slot_key(publish_at)) or {}
    topic = batch.get("topic")
    return topic if isinstance(topic, dict) and topic.get("topic") else None


def mark_batch_approved(publish_at_iso: str, how: str) -> None:
    """how: 'manual' | 'auto' | 'none' (every idea was skipped)."""
    with _lock():
        config = load_config()
        key = _slot_key(datetime.fromisoformat(publish_at_iso).astimezone().replace(tzinfo=None))
        if key in config["batches"]:
            config["batches"][key]["approved"] = how
            write_json_atomic(CONFIG_FILE, config)


def overdue_batches(config: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """Batches nobody approved within auto_approve_minutes (0 disables auto-approval)."""
    minutes = int(config.get("auto_approve_minutes") or 0)
    if minutes <= 0:
        return []
    now = now or datetime.now()
    result = []
    for batch in config.get("batches", {}).values():
        if batch.get("approved") or not batch.get("draft_ids"):
            continue
        try:
            sent_at = datetime.fromisoformat(batch["sent_at"])
        except (KeyError, ValueError):
            continue
        if now - sent_at >= timedelta(minutes=minutes):
            result.append(batch)
    return result


def viral_score(idea: dict[str, Any]) -> float:
    try:
        return float(idea.get("viral_score"))
    except (TypeError, ValueError):
        return 0.0


def pick_best(drafts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Highest Gemini viral_score among still-pending drafts (first one wins a tie)."""
    pending = [d for d in drafts if d.get("state") == "pending" and isinstance(d.get("idea"), dict)]
    if not pending:
        return None
    return max(pending, key=lambda d: viral_score(d["idea"]))  # max keeps the first on ties


def schedule_for(publish_at: datetime, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The "schedule" block stored on a draft / queue item."""
    config = config or load_config()
    return {
        "publish_at": publish_at.astimezone().isoformat(timespec="seconds"),
        "platforms": ["youtube"],
        "remove_logo": logo_mode(config) != "none",
        "logo_mode": logo_mode(config),
        "append_outro": bool(config.get("append_outro", True)),
    }


def describe_time(iso: str) -> str:
    """'19:00 hôm nay' / '07:30 ngày mai' / '19:00 25/11'."""
    when = datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)
    today = datetime.now().date()
    day = (
        "hôm nay" if when.date() == today
        else "ngày mai" if when.date() == today + timedelta(days=1)
        else when.strftime("%d/%m")
    )
    return f"{when:%H:%M} {day}"


def youtube_publish_at(schedule: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """UTC publish time for YouTube, or None when the slot already passed (publish right away)."""
    try:
        when = datetime.fromisoformat(str(schedule.get("publish_at")))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.astimezone()
    now = now or datetime.now(timezone.utc)
    # YouTube needs publishAt comfortably in the future.
    return when.astimezone(timezone.utc) if when - now > timedelta(minutes=2) else None


def prepare_video(video_path: Path, schedule: dict[str, Any]) -> Path:
    """Remove the Gemini sparkle — or cover it with the sticker — as the schedule asks;
    fall back to the original if the logo isn't there."""
    mode = logo_mode(schedule)
    if mode == "none":
        return video_path
    from core.watermark import WatermarkError, cover_logo_with_sticker, remove_gemini_watermark

    try:
        if mode == "sticker":
            return cover_logo_with_sticker(video_path).output
        return remove_gemini_watermark(video_path).output
    except WatermarkError as exc:
        LOGGER.warning("Không %s logo ✦ (%s); đăng bản gốc.", "che" if mode == "sticker" else "xóa", exc)
        return video_path


def add_outro(video_path: Path, schedule: dict[str, Any]) -> tuple[Path, bool]:
    """Append the cleaned subscribe outro (core.outro); on any problem post without it."""
    if not schedule.get("append_outro", True):
        return video_path, False
    from core.outro import append_outro

    try:
        return append_outro(video_path), True
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Không ghép được outro (%s); đăng không có outro.", exc)
        return video_path, False


def publish_scheduled(item: dict[str, Any], video_path: Path) -> dict[str, Any]:
    """Upload a rendered scheduled item to YouTube (publishAt = its slot). Returns the status."""
    from core.social_uploader import publish_to_social, parse_hashtags

    schedule = item.get("schedule") or {}
    idea = item.get("idea") or {}
    post = (idea.get("social_posts") or {}).get("youtube") or {}
    publish_at = youtube_publish_at(schedule)
    final_path, with_outro = add_outro(prepare_video(Path(video_path), schedule), schedule)
    configs = {
        "youtube": {
            "enabled": True,
            "title": str(post.get("title") or idea.get("title") or final_path.stem),
            "caption": str(post.get("caption") or ""),
            "hashtags": parse_hashtags(str(post.get("hashtags") or "")),
            "publish_at": publish_at,
        },
        "tiktok": {"enabled": False},
        "facebook": {"enabled": False},
    }
    statuses = publish_to_social(str(final_path), title=str(idea.get("title") or ""), platform_configs=configs)
    status = dict(statuses.get("youtube") or {})
    status["video"] = final_path.name
    status["outro"] = with_outro
    status["publish_at"] = publish_at.isoformat() if publish_at else None
    return status
