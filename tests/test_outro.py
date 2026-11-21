from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import outro, publish_schedule as ps

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _clip(path: Path, size: str, seconds: float, audio: bool) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class OutroTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.source_dir = self.dir / "skill"
        self.source_dir.mkdir()
        self.patches = [
            patch.object(outro, "OUTRO_SEARCH_DIR", self.source_dir),
            patch.object(outro, "CACHE_DIR", self.dir / "cache"),
            patch.dict("os.environ", {"YOUTUBE_OUTRO_FILE": ""}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_outro_is_cleaned_once_and_reused_until_the_source_changes(self) -> None:
        source = self.source_dir / "Monkey_inviting_subscrip_1080p.mp4"
        _clip(source, "1080x1920", 1, audio=True)
        (self.source_dir / "other.mp4").write_bytes(b"not an outro")
        self.assertEqual(outro.find_outro_source(), source)

        def fake_remove(src, dst):
            Path(dst).write_bytes(Path(src).read_bytes())
            calls.append(src)

        calls: list[Path] = []
        with patch("core.watermark.remove_gemini_watermark", side_effect=fake_remove):
            first = outro.clean_outro()
            second = outro.clean_outro()
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)  # cached
            _clip(source, "1080x1920", 2, audio=True)  # outro replaced -> cleaned again
            outro.clean_outro()
        self.assertEqual(len(calls), 2)

    def test_append_matches_main_video_size_and_adds_durations(self) -> None:
        main = self.dir / "main.mp4"
        tail = self.dir / "tail.mp4"
        _clip(main, "720x1280", 2, audio=False)  # no audio track: a silent one is generated
        _clip(tail, "1080x1920", 1, audio=True)
        result = outro.append_outro(main, outro=tail)
        self.assertEqual(result, self.dir / "main-outro.mp4")
        info = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
             "-of", "json", str(result)], capture_output=True, text=True, check=True).stdout)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (720, 1280))
        self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))
        self.assertAlmostEqual(float(info["format"]["duration"]), 3.0, delta=0.15)


class ScheduledOutroTests(unittest.TestCase):
    def test_outro_follows_the_schedule_flag_and_never_blocks_posting(self) -> None:
        video = Path("clip.mp4")
        with patch("core.outro.append_outro", return_value=Path("clip-outro.mp4")) as append:
            self.assertEqual(ps.add_outro(video, {"append_outro": True}), (Path("clip-outro.mp4"), True))
            self.assertEqual(ps.add_outro(video, {"append_outro": False}), (video, False))
        append.assert_called_once_with(video)
        with patch("core.outro.append_outro", side_effect=outro.OutroError("no outro")):
            self.assertEqual(ps.add_outro(video, {}), (video, False))


if __name__ == "__main__":
    unittest.main()
