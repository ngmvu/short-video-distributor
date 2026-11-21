"""Subscribe outro appended to videos posted on YouTube.

The outro (Flow clip "Monkey and man inviting subscribe…") carries the Gemini sparkle too, so it is
cleaned ONCE and cached in storage/outro/; the cache is rebuilt only when the source file changes.

Source: YOUTUBE_OUTRO_FILE (path) or the newest "*subscri*.mp4" in video-hai-two-shot-creator/
(a "_1080p" copy is preferred). Appending re-encodes both parts to the main video's size / fps /
audio format, so 720p and 1080p main videos both work.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from fractions import Fraction
from pathlib import Path

from filelock import FileLock

from core.config import ROOT_DIR, STORAGE_DIR

LOGGER = logging.getLogger("short_video_distributor.outro")
OUTRO_SEARCH_DIR = ROOT_DIR / "video-hai-two-shot-creator"
CACHE_DIR = STORAGE_DIR / "outro"
OUTRO_SUFFIX = "-outro"


class OutroError(RuntimeError):
    pass


def find_outro_source() -> Path | None:
    configured = (os.getenv("YOUTUBE_OUTRO_FILE") or "").strip().strip('"')
    if configured:
        path = Path(configured)
        path = path if path.is_absolute() else ROOT_DIR / path
        return path if path.is_file() else None
    candidates = [p for p in OUTRO_SEARCH_DIR.glob("*.mp4") if "subscri" in p.name.lower()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: ("1080p" in p.name.lower(), p.stat().st_mtime))


def clean_outro(source: Path | None = None) -> Path:
    """Logo-free copy of the outro, made once and reused while the source is unchanged."""
    from core.watermark import WatermarkError, remove_gemini_watermark

    source = source or find_outro_source()
    if source is None or not source.is_file():
        raise OutroError(f"Không tìm thấy video outro trong {OUTRO_SEARCH_DIR} (hoặc YOUTUBE_OUTRO_FILE).")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached, meta_file = CACHE_DIR / "outro-nologo.mp4", CACHE_DIR / "outro.json"
    stat = source.stat()
    fingerprint = {"source": str(source.resolve()), "size": stat.st_size, "mtime": int(stat.st_mtime)}
    with FileLock(str(CACHE_DIR / "outro.lock"), timeout=600):
        try:
            if cached.is_file() and json.loads(meta_file.read_text(encoding="utf-8")) == fingerprint:
                return cached
        except (OSError, ValueError):
            pass
        LOGGER.info("Xóa logo ✦ cho outro (chỉ làm 1 lần): %s", source.name)
        try:
            remove_gemini_watermark(source, cached)
        except WatermarkError as exc:
            # No sparkle found (e.g. a clean outro was supplied): use it as is.
            LOGGER.warning("Outro: %s — dùng nguyên bản.", exc)
            cached.write_bytes(source.read_bytes())
        meta_file.write_text(json.dumps(fingerprint, ensure_ascii=False), encoding="utf-8")
        return cached


def _probe(path: Path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise OutroError(f"Không đọc được video {path.name}") from exc
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise OutroError(f"{path.name} không có hình.")
    fps = video.get("r_frame_rate") or "24/1"
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": str(Fraction(fps)) if fps != "0/0" else "24",
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        "duration": float(data.get("format", {}).get("duration") or 0),
    }


def append_outro(video: Path, outro: Path | None = None, dst: Path | None = None) -> Path:
    """video + clean outro → "<stem>-outro.mp4" next to the video (same size/fps as the video)."""
    video = Path(video)
    outro = outro or clean_outro()
    dst = dst or video.with_name(f"{video.stem}{OUTRO_SUFFIX}.mp4")
    main, tail = _probe(video), _probe(outro)
    w, h, fps = main["width"], main["height"], main["fps"]
    fit = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
           f"setsar=1,fps={fps},format=yuv420p")
    audio_fmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    parts = []
    for index, info in enumerate((main, tail)):
        parts.append(f"[{index}:v]{fit}[v{index}]")
        if info["has_audio"]:
            parts.append(f"[{index}:a]{audio_fmt}[a{index}]")
        else:  # silent track so concat always has audio
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={info['duration']:.3f}[a{index}]")
    parts.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    tmp = dst.with_name(dst.stem + ".part.mp4")
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(outro),
        "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-crf", os.getenv("WATERMARK_CRF", "16"), "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise OutroError(f"ffmpeg ghép outro lỗi: {proc.stderr.decode(errors='replace')[-300:]}")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    LOGGER.info("Đã ghép outro vào %s (%.1fs + %.1fs).", dst.name, main["duration"], tail["duration"])
    return dst
