from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from core.config import STORAGE_DIR
from core.storage import write_json_atomic

LOGGER = logging.getLogger("short_video_distributor")

GOOGLE_TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo=VN"
TREND_CACHE_FILE = STORAGE_DIR / "trend_radar.json"
TREND_CACHE_TTL = timedelta(hours=24)
_GOOGLE_TRENDS_NAMESPACE = "https://trends.google.com/trending/rss"
_SENSITIVE_TERMS = (
    "hy sinh",
    "tử vong",
    "thiệt mạng",
    "qua đời",
    "mất tích",
    "chết",
    "tự tử",
    "tai nạn",
    "cháy nổ",
    "cháy lớn",
    "lũ quét",
    "sạt lở",
    "động đất",
    "bão lũ",
    "thảm sát",
    "bạo lực",
    "bắt cóc",
    "tấn công",
    "chiến tranh",
    "xung đột",
    "bị chìm",
    "hỏa hoạn",
)
_FIT_GROUPS = (
    ("daily_life", ("giá", "lương", "vé", "xe", "quán", "ăn", "mua", "bán")),
    ("technology", ("ai", "công nghệ", "điện thoại", "mạng xã hội", "tiktok")),
    ("entertainment", ("phim", "nhạc", "ca sĩ", "bóng đá", "du lịch", "meme")),
    ("seasonal", ("tết", "ngày lễ", "nghỉ lễ", "khai giảng")),
)


class TrendRadarError(RuntimeError):
    """Raised when Google Trends data cannot be fetched or parsed."""


@dataclass(frozen=True)
class TrendSignal:
    title: str
    approximate_traffic: str
    published_at: str
    source_url: str
    related_sources: tuple[str, ...]
    source_rank: int
    channel_fit_score: int
    fetched_at: str


def _traffic_value(raw: str) -> int:
    match = re.search(r"[\d,.]+", raw.replace(",", ""))
    if not match:
        return 0
    try:
        return int(float(match.group(0)))
    except ValueError:
        return 0


def _is_sensitive(title: str, related_titles: list[str]) -> bool:
    text = " ".join((title, *related_titles)).casefold()
    return any(term in text for term in _SENSITIVE_TERMS)


def _channel_fit_score(title: str) -> int:
    normalized = title.casefold()
    matched_groups = sum(
        any(term in normalized for term in terms)
        for _, terms in _FIT_GROUPS
    )
    return min(100, matched_groups * 25)


def _parse_feed(xml_content: bytes, fetched_at: str) -> list[TrendSignal]:
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise TrendRadarError("Google Trends returned invalid RSS XML.") from exc

    channel = root.find("./channel")
    if channel is None:
        raise TrendRadarError("Google Trends RSS did not contain a channel.")

    ns = {"ht": _GOOGLE_TRENDS_NAMESPACE}
    signals: list[TrendSignal] = []
    for source_rank, item in enumerate(channel.findall("item"), start=1):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue

        related_titles: list[str] = []
        related_sources: list[str] = []
        for news_item in item.findall("ht:news_item", ns):
            related_title = (news_item.findtext("ht:news_item_title", default="", namespaces=ns) or "").strip()
            related_url = (news_item.findtext("ht:news_item_url", default="", namespaces=ns) or "").strip()
            if related_title:
                related_titles.append(related_title)
            if related_url:
                related_sources.append(related_url)
        if _is_sensitive(title, related_titles):
            continue

        traffic = (
            item.findtext("ht:approx_traffic", default="", namespaces=ns) or ""
        ).strip()
        published_at = (item.findtext("pubDate") or "").strip()
        signals.append(
            TrendSignal(
                title=title,
                approximate_traffic=traffic,
                published_at=published_at,
                source_url=GOOGLE_TRENDS_RSS_URL,
                related_sources=tuple(dict.fromkeys(related_sources[:3])),
                source_rank=source_rank,
                channel_fit_score=_channel_fit_score(title),
                fetched_at=fetched_at,
            )
        )

    signals.sort(
        key=lambda signal: (
            -signal.channel_fit_score,
            -_traffic_value(signal.approximate_traffic),
            signal.source_rank,
        )
    )
    return signals


def _read_cache(path: Path, now: datetime) -> list[TrendSignal] | None:
    try:
        cached: Any = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if now - fetched_at >= TREND_CACHE_TTL:
            return None

        signals = [
            TrendSignal(
                **{
                    **entry,
                    "related_sources": tuple(entry.get("related_sources", ())),
                }
            )
            for entry in cached["trends"]
        ]
        return signals
    except FileNotFoundError:
        return None
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring invalid Google Trends cache at %s: %s", path, exc)
        return None


def get_trend_signals(limit: int = 20) -> list[TrendSignal]:
    """Get cached daily Google Trends signals for Vietnam, ranked for channel fit."""
    if not 1 <= limit <= 50:
        raise ValueError("Trend signal limit must be between 1 and 50.")

    now = datetime.now(timezone.utc)
    cached = _read_cache(TREND_CACHE_FILE, now)
    if cached is not None:
        return cached[:limit]

    fetched_at = now.isoformat()
    try:
        response = requests.get(
            GOOGLE_TRENDS_RSS_URL,
            headers={"User-Agent": "short-video-distributor/1.0"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise TrendRadarError("Could not fetch Google Trends RSS for Vietnam.") from exc

    signals = _parse_feed(response.content, fetched_at)
    if not signals:
        raise TrendRadarError(
            "Google Trends RSS contained no usable, non-sensitive topics."
        )

    write_json_atomic(
        TREND_CACHE_FILE,
        {
            "source": GOOGLE_TRENDS_RSS_URL,
            "fetched_at": fetched_at,
            "trends": [asdict(signal) for signal in signals],
        },
    )
    return signals[:limit]
