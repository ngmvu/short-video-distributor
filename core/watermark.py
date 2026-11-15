"""Remove the visible Gemini/Veo sparkle (✦) from a downloaded Flow video, without cropping.

The sparkle is a fixed, semi-transparent white overlay: watermarked = a·255 + (1 − a)·original,
with a per-pixel opacity map `a` (≈0.29 inside the star, anti-aliased edge, slight dark halo).
Knowing `a`, the original pixels are recovered exactly: original = (watermarked − a·255) / (1 − a)
("reverse alpha blending"). Video compression smears the star's outline a little, so a 2–3 px band
along the edge is then filled from the restored pixels around it.

The opacity maps in core/assets/ were measured from ~5000 frames of real Flow downloads
(1080×1920 and 720×1280). Other sizes (e.g. 4K) reuse the nearest map scaled, and the exact
position is always found by template matching, so a few pixels of drift between sizes is fine.
Only the region around the logo goes through Python; ffmpeg overlays it back onto the video.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

LOGGER = logging.getLogger("short_video_distributor.watermark")
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
LOGO_LEVEL = 255.0
# Calibrated profiles: (width, height) -> (mask file, top-left of the mask in the frame).
PROFILES: dict[tuple[int, int], tuple[str, tuple[int, int]]] = {
    (1080, 1920): ("gemini_sparkle_1080x1920.npy", (858, 1698)),
    (720, 1280): ("gemini_sparkle_720x1280.npy", (572, 1132)),
}
MIN_MATCH_SCORE = 0.4  # normalized cross-correlation of the mask with the mean frame
NOLOGO_SUFFIX = "-nologo"


class WatermarkError(RuntimeError):
    """The video could not be processed (bad file, ffmpeg failure, logo not found)."""


@dataclass
class RemovalResult:
    output: Path
    x: int
    y: int
    score: float
    frames: int
    width: int
    height: int


@dataclass
class _VideoInfo:
    width: int
    height: int
    fps: str


def _probe(path: Path) -> _VideoInfo:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout
        stream = json.loads(out)["streams"][0]
        return _VideoInfo(int(stream["width"]), int(stream["height"]), str(stream["r_frame_rate"]))
    except (subprocess.SubprocessError, OSError, KeyError, IndexError, ValueError) as exc:
        raise WatermarkError(f"Không đọc được thông tin video: {path.name}") from exc


def _resize(mask: np.ndarray, size: int) -> np.ndarray:
    """Bilinear resize of a square map to size×size."""
    src = mask.shape[0]
    coords = (np.arange(size) + 0.5) * src / size - 0.5
    lo = np.clip(np.floor(coords).astype(int), 0, src - 1)
    hi = np.clip(lo + 1, 0, src - 1)
    t = np.clip(coords - lo, 0, 1)
    rows = mask[lo] * (1 - t)[:, None] + mask[hi] * t[:, None]
    return (rows[:, lo] * (1 - t)[None, :] + rows[:, hi] * t[None, :]).astype(np.float32)


def _mask_for(width: int, height: int) -> tuple[np.ndarray, int, int]:
    """Opacity map and its expected top-left corner for this frame size."""
    if (width, height) in PROFILES:
        name, (x, y) = PROFILES[(width, height)]
        return np.load(ASSETS_DIR / name), x, y
    ref_w, ref_h = min(PROFILES, key=lambda size: abs(size[0] - width))
    name, (ref_x, ref_y) = PROFILES[(ref_w, ref_h)]
    ref = np.load(ASSETS_DIR / name)
    scale = width / ref_w
    size = max(8, int(round(ref.shape[0] * scale / 2)) * 2)
    center_x = (ref_x + ref.shape[0] / 2) * scale
    center_y = (ref_y + ref.shape[0] / 2) * height / ref_h
    return _resize(ref, size), int(round(center_x - size / 2)), int(round(center_y - size / 2))


def _box_sum(values: np.ndarray, radius: int) -> np.ndarray:
    """Sum over a (2r+1)² window around each pixel (zero padding)."""
    pad = ((radius + 1, radius), (radius + 1, radius)) + ((0, 0),) * (values.ndim - 2)
    c = np.pad(values, pad).cumsum(0).cumsum(1)
    k = 2 * radius + 1
    return c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]


def _grow(mask: np.ndarray, steps: int, shrink: bool = False) -> np.ndarray:
    for _ in range(steps):
        p = np.pad(mask, 1, constant_values=shrink)
        neighbours = [p[1:-1, 1:-1], p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]]
        mask = np.logical_and.reduce(neighbours) if shrink else np.logical_or.reduce(neighbours)
    return mask


def _locate(mean_luma: np.ndarray, mask: np.ndarray) -> tuple[int, int, float]:
    """Best top-left offset of `mask` inside the search window, by normalized cross-correlation."""
    size = mask.shape[0]
    template = mask - mask.mean()
    template /= np.linalg.norm(template) or 1.0
    best = (0, 0, -1.0)
    for dy in range(mean_luma.shape[0] - size + 1):
        for dx in range(mean_luma.shape[1] - size + 1):
            patch = mean_luma[dy:dy + size, dx:dx + size]
            patch = patch - patch.mean()
            norm = np.linalg.norm(patch)
            if norm < 1e-6:
                continue
            score = float((patch * template).sum() / norm)
            if score > best[2]:
                best = (dx, dy, score)
    return best


def _read_frames(src: Path, x: int, y: int, w: int, h: int) -> np.ndarray:
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(src), "-vf", f"format=rgb24,crop={w}:{h}:{x}:{y}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise WatermarkError(f"ffmpeg không đọc được video: {exc.stderr.decode(errors='replace')[-300:]}") from exc
    frames = np.frombuffer(raw, np.uint8)
    if frames.size < w * h * 3:
        raise WatermarkError("Video không có khung hình nào.")
    return frames[: frames.size // (w * h * 3) * (w * h * 3)].reshape(-1, h, w, 3)


def default_output(src: Path) -> Path:
    return src.with_name(f"{src.stem}{NOLOGO_SUFFIX}{src.suffix or '.mp4'}")


def remove_gemini_watermark(src: str | Path, dst: str | Path | None = None, *, force: bool = False) -> RemovalResult:
    """Write a copy of `src` with the sparkle removed (same size, audio copied).

    Raises WatermarkError when the logo isn't found (score below MIN_MATCH_SCORE) unless `force`.
    """
    src = Path(src)
    if not src.is_file():
        raise WatermarkError(f"Không tìm thấy file: {src}")
    dst = Path(dst) if dst else default_output(src)
    info = _probe(src)
    mask, guess_x, guess_y = _mask_for(info.width, info.height)
    size = mask.shape[0]
    margin = max(6, info.width // 100)
    # Even window origin, so rounding the match down to even never leaves the window.
    win_x = max(0, min(guess_x - margin, info.width - size - 2 * margin)) & ~1
    win_y = max(0, min(guess_y - margin, info.height - size - 2 * margin)) & ~1
    win = size + 2 * margin
    if win > min(info.width, info.height):
        raise WatermarkError(f"Video quá nhỏ ({info.width}×{info.height}).")

    window = _read_frames(src, win_x, win_y, win, win)
    mean_luma = window.astype(np.float32).mean(axis=(0, 3))
    dx, dy, score = _locate(mean_luma, np.maximum(mask, 0))
    LOGGER.info("Logo Gemini: %s %dx%d, vị trí (%d,%d), độ khớp %.2f",
                src.name, info.width, info.height, win_x + dx, win_y + dy, score)
    if score < MIN_MATCH_SCORE and not force:
        raise WatermarkError(
            f"Không thấy logo ✦ ở góc dưới phải (độ khớp {score:.2f} < {MIN_MATCH_SCORE}). "
            "Có thể video không có logo."
        )

    # x/y even so the RGB patch lines up with yuv420 chroma when overlaid back.
    x, y = (win_x + dx) & ~1, (win_y + dy) & ~1
    mask = np.roll(mask, ((win_y + dy) - y, (win_x + dx) - x), axis=(0, 1))
    rx, ry = x - win_x, y - win_y
    roi = window[:, ry:ry + size, rx:rx + size].astype(np.float32)

    alpha = mask[None, ..., None]
    restored = (roi - alpha * LOGO_LEVEL) / (1 - alpha)
    scale = info.width / 1080
    band_width = max(1, round(2 * scale))
    core = mask > 0.15
    band = _grow(core, band_width) & ~_grow(core, band_width, shrink=True)
    keep = (~band).astype(np.float32)
    radius = max(2, round(4 * scale))
    weight = np.maximum(_box_sum(keep, radius), 1e-3)[..., None]
    for i in range(restored.shape[0]):
        fill = _box_sum(restored[i] * keep[..., None], radius) / weight
        restored[i][band] = fill[band]
    patch = np.clip(restored, 0, 255).round().astype(np.uint8)

    crf = os.getenv("WATERMARK_CRF", "16")
    tmp = dst.with_name(dst.stem + ".part" + dst.suffix)
    fps = str(Fraction(info.fps)) if info.fps and info.fps != "0/0" else "24"
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-i", str(src),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size}x{size}", "-r", fps, "-i", "-",
        "-filter_complex", f"[0:v][1:v]overlay={x}:{y}:eof_action=pass:format=auto[v]",
        "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", crf, "-preset", "medium",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, input=patch.tobytes(), capture_output=True)
        if proc.returncode != 0:
            raise WatermarkError(f"ffmpeg ghi video lỗi: {proc.stderr.decode(errors='replace')[-300:]}")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    return RemovalResult(dst, x, y, score, int(patch.shape[0]), info.width, info.height)
