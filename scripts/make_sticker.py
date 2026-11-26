"""Turn a sticker picture on a white background into a transparent die-cut PNG for logo covering.

    python scripts/make_sticker.py anh_nhan_dan.png core/assets/sticker_khi_chu.png

Keeps the drawing (dark ink lines + coloured areas, with everything they enclose), drops the white
background and the grey drop shadow, and draws a clean white border around it like a die-cut
sticker. Needs only ffmpeg + numpy.
"""
from __future__ import annotations

import argparse
import subprocess

import numpy as np

MAX_SIDE = 640  # the sticker is shown at ~100–300 px in a video, no need for more


def _grow(mask: np.ndarray, steps: int, shrink: bool = False) -> np.ndarray:
    for _ in range(steps):
        p = np.pad(mask, 1, constant_values=shrink)
        n = [p[1:-1, 1:-1], p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]]
        mask = np.logical_and.reduce(n) if shrink else np.logical_or.reduce(n)
    return mask


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    outside = np.zeros_like(mask)
    outside[0, :] = outside[-1, :] = outside[:, 0] = outside[:, -1] = True
    outside &= ~mask
    while True:
        p = np.pad(outside, 1)
        grown = (p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]) & ~mask
        if (grown == outside).all():
            return ~outside
        outside = grown


def make_sticker(src: str, dst: str, border: int = 7) -> tuple[int, int]:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", src], capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")
    width, height = int(probe[0]), int(probe[1])
    scale = min(1.0, MAX_SIDE / max(width, height))
    w, h = max(2, round(width * scale)), max(2, round(height * scale))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", src, "-vf", f"scale={w}:{h}:flags=area",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True,
    ).stdout
    rgb = np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(np.int16)
    lo, hi = rgb.min(2), rgb.max(2)

    ink = (lo < 110) | (hi - lo > 40)  # dark lines + clear colours, not the light-grey shadow
    body = _fill_holes(_grow(_grow(ink, 10), 10, shrink=True))  # close gaps, fill the inside
    sticker = _grow(body, border)
    out = rgb.copy()
    out[sticker & ~body] = 255  # white die-cut border

    alpha = sticker.astype(np.float32)
    p = np.pad(alpha, 1, mode="edge")  # 1 px soft edge
    alpha = (p[1:-1, 1:-1] * 4 + p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]) / 8
    ys, xs = np.where(sticker)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    rgba = np.dstack([out.astype(np.uint8), (alpha * 255).round().astype(np.uint8)])[y0:y1, x0:x1]
    oh, ow = rgba.shape[:2]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{ow}x{oh}",
         "-i", "-", dst], input=np.ascontiguousarray(rgba).tobytes(), check=True,
    )
    return ow, oh


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("src", help="ảnh nhãn dán trên nền trắng (png/jpg)")
    parser.add_argument("dst", help="file PNG trong suốt đầu ra")
    parser.add_argument("--border", type=int, default=7, help="độ dày viền trắng (px)")
    args = parser.parse_args()
    print("Đã tạo nhãn dán %dx%d" % make_sticker(args.src, args.dst, args.border))


if __name__ == "__main__":
    main()
