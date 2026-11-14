"""Transcribe the speech of TikTok / YouTube video links and send it to Telegram.

Usage:
    python -m core.link_transcriber https://vt.tiktok.com/ZSp4Stubm/ [more links] --send-telegram
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from core.config import ROOT_DIR, STORAGE_DIR, get_setting
from core.storage import read_json
from core.transcription import TRANSCRIPT_DIR, load_whisper_model, transcribe_media

# Child logger: "/log" on Telegram filters transcription lines by this name.
LOGGER = logging.getLogger("short_video_distributor.thoai_hot")

DOWNLOAD_DIR = STORAGE_DIR / "downloads" / "link_transcribe"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_LINKS_PER_REQUEST = 10
TELEGRAM_TEXT_LIMIT = 3900
# TikTok exposes the post's linked "sound" (often background music) as format_id "audio";
# it is not the video's own soundtrack, so skip it and fall back to the video file.
AUDIO_FORMAT = "bestaudio[format_id!=audio]/best[acodec!=none][vcodec!=none]/best[acodec!=none]"
PLATFORM_LABELS = {"youtube": "YouTube", "tiktok": "TikTok"}
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")


class TranscribeError(RuntimeError):
    """A step failed with a message that can be shown to the user."""


@dataclass
class VideoInfo:
    platform: str
    url: str
    video_id: str = ""
    title: str = ""
    uploader: str = ""
    view_count: int = 0
    duration: float = 0.0


@dataclass
class TranscriptResult:
    video: VideoInfo
    transcript_path: Path | None = None
    language: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------------- links


def video_platform(url: str) -> str | None:
    """Return "tiktok"/"youtube" for a single-video link, otherwise None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path
    if host.endswith("tiktok.com"):
        if "/video/" in path or host.startswith(("vm.", "vt.")) or path.startswith("/t/"):
            return "tiktok"
        return None
    if host == "youtu.be" or (
        host.endswith("youtube.com") and path.startswith(("/watch", "/shorts/"))
    ):
        return "youtube"
    return None


def extract_video_links(text: str) -> tuple[list[str], list[str]]:
    """Split the URLs found in text into (supported video links, ignored links)."""
    links: list[str] = []
    ignored: list[str] = []
    for raw in _URL_PATTERN.findall(text):
        url = raw.rstrip(".,;)")
        if video_platform(url) is None:
            ignored.append(url)
        elif url not in links:
            links.append(url)
    return links, ignored


def is_only_links(text: str) -> bool:
    """True when the message consists only of URLs (used to auto-detect a pasted link)."""
    tokens = text.split()
    return bool(tokens) and all(_URL_PATTERN.fullmatch(token.rstrip(".,;)")) for token in tokens)


# --------------------------------------------------------------------------- download


def _yt_dlp() -> Any:
    try:
        import yt_dlp
    except ImportError as exc:
        raise TranscribeError(
            "yt-dlp chưa được cài. Chạy: python -m pip install -r requirements.txt"
        ) from exc
    return yt_dlp


def _base_options() -> dict[str, Any]:
    options: dict[str, Any] = {"quiet": True, "no_warnings": True, "noprogress": True}
    cookies = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if cookies:
        path = Path(cookies).expanduser()
        path = path if path.is_absolute() else ROOT_DIR / path
        if path.is_file():
            options["cookiefile"] = str(path)
        else:
            LOGGER.warning("YTDLP_COOKIES_FILE không tồn tại: %s", path)
    return options


def _download_error_text(exc: Exception) -> str:
    text = str(exc).replace("ERROR: ", "")
    if "confirm you" in text and "not a bot" in text:
        return (
            "YouTube yêu cầu đăng nhập cho IP này. Xuất cookies YouTube của bạn ra file "
            "cookies.txt và đặt YTDLP_COOKIES_FILE trong .env (xem README)."
        )
    return text.split(" Use --cookies")[0][:300]


def download_audio(url: str, download_dir: Path = DOWNLOAD_DIR) -> tuple[Path, dict[str, Any]]:
    """Download one video's soundtrack. Returns (file path, yt-dlp info)."""
    yt_dlp = _yt_dlp()
    download_dir.mkdir(parents=True, exist_ok=True)
    options = {
        **_base_options(),
        "noplaylist": True,
        "format": AUDIO_FORMAT,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "outtmpl": str(download_dir / "%(extractor_key)s_%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": False,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise TranscribeError(f"Không tải được video: {_download_error_text(exc)}") from exc
    downloads = info.get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads else Path(ydl.prepare_filename(info))
    if not path.is_file():
        raise TranscribeError("Video vượt giới hạn dung lượng hoặc không có âm thanh.")
    LOGGER.info(
        "Đã tải %s: format=%s (audio %s), %.1f MB.",
        url,
        info.get("format_id"),
        info.get("acodec"),
        path.stat().st_size / 1024 / 1024,
    )
    return path, info


# --------------------------------------------------------------------------- pipeline

_model_cache: dict[str, Any] = {}
_model_lock = threading.Lock()


def get_whisper_model(model_size: str) -> Any:
    with _model_lock:
        if model_size not in _model_cache:
            LOGGER.info("Đang nạp model Whisper '%s'…", model_size)
            _model_cache[model_size] = load_whisper_model(model_size)
        return _model_cache[model_size]


def _apply_info(video: VideoInfo, info: dict[str, Any]) -> None:
    video.video_id = str(info.get("id") or video.video_id)
    video.title = str(info.get("title") or video.title).strip()
    video.uploader = str(info.get("uploader") or info.get("channel") or video.uploader).strip()
    video.view_count = int(info.get("view_count") or video.view_count or 0)
    video.duration = float(info.get("duration") or video.duration or 0)


def transcribe_link(
    url: str,
    model: Any,
    model_size: str,
    language: str | None,
    download_dir: Path = DOWNLOAD_DIR,
    output_dir: Path = TRANSCRIPT_DIR,
) -> TranscriptResult:
    video = VideoInfo(platform=video_platform(url) or "tiktok", url=url)
    result = TranscriptResult(video=video)
    try:
        media_path, info = download_audio(url, download_dir)
        _apply_info(video, info)
        try:
            result.transcript_path = transcribe_media(
                model,
                media_path,
                url,
                model_size,
                language,
                output_dir,
                extra={
                    "platform": video.platform,
                    "video_id": video.video_id,
                    "title": video.title,
                    "uploader": video.uploader,
                    "view_count": video.view_count,
                },
            )
        finally:
            media_path.unlink(missing_ok=True)
        payload = read_json(result.transcript_path, {})
        result.language = str(payload.get("language") or "")
        result.segments = list(payload.get("segments") or [])
        LOGGER.info(
            "Whisper xong %s:%s: %d đoạn thoại, ngôn ngữ %s, dài %ss.",
            video.platform,
            video.video_id,
            len(result.segments),
            result.language,
            payload.get("duration_seconds"),
        )
    except TranscribeError as exc:
        result.error = str(exc)
        LOGGER.warning("Bỏ qua %s: %s", url, exc)
    except Exception as exc:  # noqa: BLE001 - report every per-video failure to the user
        LOGGER.exception("Bóc thoại thất bại cho %s", url)
        result.error = f"{type(exc).__name__}: {exc}"[:300]
    return result


def transcribe_links(
    links: list[str],
    model_size: str | None = None,
    language: str | None = "vi",
    model: Any | None = None,
    download_dir: Path = DOWNLOAD_DIR,
    output_dir: Path = TRANSCRIPT_DIR,
) -> list[TranscriptResult]:
    if not links:
        return []
    model_size = model_size or os.getenv("WHISPER_MODEL", "small") or "small"
    whisper = model if model is not None else get_whisper_model(model_size)
    results = []
    for index, url in enumerate(links, 1):
        LOGGER.info("Bóc thoại %d/%d: %s", index, len(links), url)
        results.append(
            transcribe_link(url, whisper, model_size, language, download_dir, output_dir)
        )
    return results


# --------------------------------------------------------------------------- Telegram output


def _clock(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _compact_number(value: int) -> str:
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= size:
            return f"{value / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def format_result_messages(result: TranscriptResult, index: int = 1) -> list[str]:
    """HTML Telegram messages for one video, split to stay under the message size limit."""
    video = result.video
    label = PLATFORM_LABELS.get(video.platform, video.platform)
    title = html.escape((video.title or video.url)[:200])
    meta = []
    if video.view_count:
        meta.append(f"👁 {_compact_number(video.view_count)} lượt xem")
    if video.duration:
        meta.append(f"⏱ {_clock(video.duration)}")
    if video.uploader:
        meta.append(f"👤 {html.escape(video.uploader[:60])}")
    header = [f"🎵 <b>#{index} [{label}] {title}</b>"]
    if meta:
        header.append(" · ".join(meta))
    header.append(f'🔗 <a href="{html.escape(video.url, quote=True)}">Xem video</a>')

    if result.error:
        header.append(f"❌ {html.escape(result.error)}")
        return ["\n".join(header)]

    lines = [
        f"<code>[{_clock(segment.get('start', 0))}]</code> {html.escape(str(segment.get('text', '')))}"
        for segment in result.segments
        if str(segment.get("text", "")).strip()
    ]
    if not lines:
        header.append("🔇 <i>Không nghe thấy lời thoại (có thể chỉ có nhạc).</i>")
        return ["\n".join(header)]

    header.append("💬 <b>Lời thoại:</b>")
    messages: list[str] = []
    current = "\n".join(header)
    for line in lines:
        if len(current) + len(line) + 1 > TELEGRAM_TEXT_LIMIT:
            messages.append(current)
            current = f"💬 <i>(tiếp #{index})</i>"
        current += "\n" + line
    messages.append(current)
    return messages


def build_telegram_messages(results: list[TranscriptResult]) -> list[str]:
    messages: list[str] = []
    for index, result in enumerate(results, 1):
        messages.extend(format_result_messages(result, index))
    ok = sum(1 for result in results if not result.error)
    messages.append(f"✅ <b>Xong:</b> bóc thoại {ok}/{len(results)} video.")
    return messages


def send_messages_sync(messages: list[str]) -> None:
    token = get_setting("TELEGRAM_BOT_TOKEN")
    chat_id = get_setting("TELEGRAM_CHAT_ID")
    for text in messages:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=60,
        )
        response.raise_for_status()


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Bóc lời thoại từ link video TikTok/YouTube bằng Whisper, gửi lên Telegram."
    )
    parser.add_argument("links", nargs="+", help="Một hoặc nhiều link video TikTok/YouTube.")
    parser.add_argument("--model", default=None, help="Whisper model (mặc định WHISPER_MODEL hoặc small).")
    parser.add_argument("--language", default="vi", help="Mã ngôn ngữ, hoặc 'auto'.")
    parser.add_argument("--send-telegram", action="store_true", help="Gửi kết quả lên Telegram.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    links, ignored = extract_video_links(" ".join(args.links))
    for url in ignored:
        LOGGER.warning("Bỏ qua link không phải video TikTok/YouTube: %s", url)
    if not links:
        parser.error("Không có link video TikTok/YouTube hợp lệ.")
    language = None if args.language.lower() == "auto" else args.language
    results = transcribe_links(links[:MAX_LINKS_PER_REQUEST], model_size=args.model, language=language)
    messages = build_telegram_messages(results)
    if args.send_telegram:
        send_messages_sync(messages)
    for message in messages:
        print(re.sub(r"<[^>]+>", "", html.unescape(message)), end="\n\n")


if __name__ == "__main__":
    main()
