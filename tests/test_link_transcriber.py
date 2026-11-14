from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import link_transcriber
from core.link_transcriber import (
    TranscribeError,
    TranscriptResult,
    VideoInfo,
    build_telegram_messages,
    extract_video_links,
    format_result_messages,
    is_only_links,
    transcribe_links,
    video_platform,
)


class LinkTests(unittest.TestCase):
    def test_video_platform(self) -> None:
        self.assertEqual(video_platform("https://vt.tiktok.com/ZSp4Stubm/"), "tiktok")
        self.assertEqual(video_platform("https://www.tiktok.com/@a/video/123?lang=vi"), "tiktok")
        self.assertEqual(video_platform("https://youtube.com/shorts/abc"), "youtube")
        self.assertEqual(video_platform("https://youtu.be/abc"), "youtube")
        self.assertIsNone(video_platform("https://www.tiktok.com/search?q=kh%E1%BB%89"))
        self.assertIsNone(video_platform("https://www.tiktok.com/@user"))
        self.assertIsNone(video_platform("https://example.com/video.mp4"))

    def test_extract_links_from_pasted_text(self) -> None:
        links, ignored = extract_video_links(
            "xem cái này https://vt.tiktok.com/A/, và https://vt.tiktok.com/A/\n"
            "https://www.tiktok.com/search?q=x https://www.tiktok.com/@u/video/9"
        )
        self.assertEqual(links, ["https://vt.tiktok.com/A/", "https://www.tiktok.com/@u/video/9"])
        self.assertEqual(ignored, ["https://www.tiktok.com/search?q=x"])

    def test_is_only_links(self) -> None:
        self.assertTrue(is_only_links("https://vt.tiktok.com/A/\nhttps://vt.tiktok.com/B/"))
        self.assertFalse(is_only_links("khỉ bán gà https://vt.tiktok.com/A/"))
        self.assertFalse(is_only_links(""))


class TranscribeLinksTests(unittest.TestCase):
    def test_uses_video_soundtrack_not_tiktok_sound(self) -> None:
        self.assertIn("format_id!=audio", link_transcriber.AUDIO_FORMAT.split("/")[0])

    def test_downloads_transcribes_and_cleans_up(self) -> None:
        model = Mock()
        model.transcribe.return_value = (
            iter([SimpleNamespace(start=1.2, end=2.0, text=" Chào bạn ")]),
            SimpleNamespace(language="vi", duration=2.0),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "TikTok_1.mp4"

            def fake_download(url: str, download_dir: Path) -> tuple[Path, dict]:
                media.write_bytes(b"audio")
                return media, {"id": "1", "title": "Tiêu đề", "view_count": 1234, "duration": 30}

            with patch.object(link_transcriber, "download_audio", side_effect=fake_download):
                results = transcribe_links(
                    ["https://vt.tiktok.com/A/"], model_size="tiny", model=model,
                    output_dir=root / "out",
                )
            payload = json.loads(results[0].transcript_path.read_text(encoding="utf-8"))

        self.assertEqual(results[0].segments, [{"start": 1.2, "end": 2.0, "text": "Chào bạn"}])
        self.assertEqual(results[0].video.title, "Tiêu đề")
        self.assertEqual(payload["view_count"], 1234)
        self.assertFalse(media.exists(), "audio file should be cleaned up")

    def test_download_error_is_reported_per_video(self) -> None:
        with patch.object(
            link_transcriber, "download_audio", side_effect=TranscribeError("Không tải được video: x")
        ):
            results = transcribe_links(["https://vt.tiktok.com/A/"], model=Mock())
        self.assertEqual(results[0].error, "Không tải được video: x")

    def test_no_links_does_not_load_model(self) -> None:
        with patch.object(link_transcriber, "get_whisper_model") as get_model:
            self.assertEqual(transcribe_links([]), [])
        get_model.assert_not_called()


class FormatTests(unittest.TestCase):
    def test_long_transcript_is_split_and_escaped(self) -> None:
        video = VideoInfo("tiktok", "https://t.co/x", title="<b>bad</b>", view_count=1_500_000)
        segments = [{"start": i, "end": i + 1, "text": "x" * 200} for i in range(60)]
        messages = format_result_messages(TranscriptResult(video=video, segments=segments))
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 4096 for message in messages))
        self.assertIn("&lt;b&gt;bad", messages[0])
        self.assertIn("1.5M", messages[0])

    def test_summary_counts_successes(self) -> None:
        ok = TranscriptResult(video=VideoInfo("tiktok", "https://a"), segments=[])
        bad = TranscriptResult(video=VideoInfo("tiktok", "https://b"), error="x")
        self.assertIn("1/2", build_telegram_messages([ok, bad])[-1])


if __name__ == "__main__":
    unittest.main()
