from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

from core import watermark
from core.tele import watermark_bot

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _make_video(path: Path, width: int, height: int, frames: int, with_logo: bool) -> np.ndarray:
    """Lossless synthetic clip: moving texture, optionally with the calibrated sparkle blended in."""
    rng = np.random.default_rng(0)
    base = rng.integers(20, 230, size=(height // 8, width // 8, 3)).astype(np.float32)
    base = np.kron(base, np.ones((8, 8, 1), np.float32))  # blocky = smooth-ish texture
    clean = np.stack([np.roll(base, 4 * i, axis=1) for i in range(frames)])
    out = clean.copy()
    if with_logo:
        mask, x, y = watermark._mask_for(width, height)
        a = mask[None, ..., None]
        size = mask.shape[0]
        out[:, y:y + size, x:x + size] = a * 255 + (1 - a) * out[:, y:y + size, x:x + size]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
         "-r", "24", "-i", "-", "-c:v", "libx264rgb", "-qp", "0", str(path)],
        input=np.clip(out, 0, 255).round().astype(np.uint8).tobytes(), check=True,
    )
    return clean


def _decode(path: Path, width: int, height: int) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3).astype(np.float32)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class RemoveWatermarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_restores_pixels_under_the_logo_without_cropping(self) -> None:
        src = self.dir / "clip.mp4"
        clean = _make_video(src, 720, 1280, 6, with_logo=True)
        result = watermark.remove_gemini_watermark(src)

        self.assertEqual(result.output, self.dir / "clip-nologo.mp4")
        self.assertEqual((result.width, result.height, result.frames), (720, 1280, 6))
        self.assertEqual((result.x, result.y), watermark.PROFILES[(720, 1280)][1])
        self.assertGreater(result.score, watermark.MIN_MATCH_SCORE)

        mask, x, y = watermark._mask_for(720, 1280)
        size = mask.shape[0]
        core = mask > 0.15
        star = np.zeros((1280, 720), bool)
        star[y:y + size, x:x + size] = core
        interior = np.zeros((1280, 720), bool)
        interior[y:y + size, x:x + size] = watermark._grow(core, 2, shrink=True)
        elsewhere = np.roll(interior, -200, axis=1)  # same shape, no logo: pure re-encode error
        before = _decode(src, 720, 1280)
        after = _decode(result.output, 720, 1280)
        self.assertEqual(after.shape, before.shape)
        error_before = np.abs(before - clean)[:, star].mean()
        error_after = np.abs(after - clean)[:, star].mean()
        # Inside the star, reverse blending recovers the pixels up to the re-encode noise.
        interior_error = np.abs(after - clean)[:, interior].mean()
        encode_error = np.abs(after - clean)[:, elsewhere].mean()
        self.assertGreater(error_before, 30)
        self.assertLess(interior_error, encode_error * 1.5 + 1)
        self.assertLess(error_after, error_before / 4)

    def test_refuses_video_without_logo_unless_forced(self) -> None:
        src = self.dir / "plain.mp4"
        _make_video(src, 720, 1280, 3, with_logo=False)
        with self.assertRaises(watermark.WatermarkError):
            watermark.remove_gemini_watermark(src)
        self.assertFalse((self.dir / "plain-nologo.mp4").exists())
        self.assertTrue(watermark.remove_gemini_watermark(src, force=True).output.exists())

    def test_other_resolutions_scale_the_nearest_mask(self) -> None:
        mask, x, y = watermark._mask_for(2160, 3840)
        ref, rx, ry = watermark._mask_for(1080, 1920)
        self.assertEqual(mask.shape[0], ref.shape[0] * 2)
        self.assertLessEqual(abs(x - 2 * rx), 2)
        self.assertLessEqual(abs(y - 2 * ry), 2)


def _run(coro):
    return asyncio.run(coro)


class WatermarkBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.storage = root / "storage"
        self.downloads = self.storage / "downloads"
        self.downloads.mkdir(parents=True)
        (self.downloads / "a-scene3.mp4").write_bytes(b"x")
        (root / "outside.mp4").write_bytes(b"x")
        self.outside = root / "outside.mp4"
        self.patches = [
            patch.object(watermark_bot, "STORAGE_DIR", self.storage),
            patch.object(watermark_bot, "DOWNLOADS_DIR", self.downloads),
        ]
        for p in self.patches:
            p.start()
        watermark_bot._waiting.clear()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        watermark_bot._waiting.clear()
        self.tmp.cleanup()

    def test_resolves_names_and_paths_only_inside_storage(self) -> None:
        expected = (self.downloads / "a-scene3.mp4").resolve()
        self.assertEqual(watermark_bot.resolve_local_video("a-scene3.mp4"), expected)
        self.assertEqual(watermark_bot.resolve_local_video(f'"{expected}"'), expected)
        self.assertIsNone(watermark_bot.resolve_local_video(str(self.outside)))
        self.assertIsNone(watermark_bot.resolve_local_video("../../outside.mp4"))
        self.assertIsNone(watermark_bot.resolve_local_video("missing.mp4"))
        self.assertIsNone(watermark_bot.resolve_local_video("chào bạn"))

    def test_text_and_video_ignored_outside_the_mode(self) -> None:
        message = SimpleNamespace(chat_id=1, video=object(), caption="", reply_text=AsyncMock())
        self.assertFalse(_run(watermark_bot.handle_text(message, "a-scene3.mp4")))
        self.assertFalse(_run(watermark_bot.handle_video(message)))
        message.reply_text.assert_not_awaited()

    def test_command_with_file_name_processes_and_sends_back(self) -> None:
        output = self.downloads / "a-scene3-nologo.mp4"
        output.write_bytes(b"done")
        status = MagicMock(edit_text=AsyncMock())
        message = SimpleNamespace(
            chat_id=1, reply_text=AsyncMock(return_value=status), get_bot=MagicMock(return_value="bot")
        )
        result = watermark.RemovalResult(output, 858, 1698, 0.94, 143, 1080, 1920)

        async def scenario() -> None:
            with (
                patch.object(watermark_bot, "remove_gemini_watermark", return_value=result) as remove,
                patch.object(watermark_bot, "send_video_file", AsyncMock()) as send,
            ):
                await watermark_bot.handle_command(message, ["a-scene3.mp4"])
                await asyncio.gather(*watermark_bot._background_tasks)
            remove.assert_called_once_with((self.downloads / "a-scene3.mp4").resolve())
            # Sent back as a playable video into the same chat.
            path, caption, bot, chat_id, _markup = send.await_args.args
            self.assertEqual((path, bot, chat_id), (output, "bot", 1))
            self.assertIn(output.name, caption)

        _run(scenario())
        self.assertIn("Đã xóa logo", status.edit_text.await_args.args[0])

    def test_too_large_upload_is_rejected_with_hint(self) -> None:
        watermark_bot._waiting.add("1")
        message = SimpleNamespace(
            chat_id=1, video=SimpleNamespace(file_size=30 * 1024 * 1024), caption="", reply_text=AsyncMock()
        )
        self.assertTrue(_run(watermark_bot.handle_video(message)))
        self.assertIn("20 MB", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
