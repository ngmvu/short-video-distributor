from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.transcription import (
    download_media,
    load_authorized_urls,
    process_url_list,
)


class TranscriptionTests(unittest.TestCase):
    def test_load_authorized_urls_skips_comments_and_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            url_list = Path(temporary) / "urls.txt"
            url_list.write_text(
                "# my authorized videos\n\nhttps://cdn.example/video.mp4\n",
                encoding="utf-8",
            )

            self.assertEqual(
                load_authorized_urls(url_list),
                ["https://cdn.example/video.mp4"],
            )

    def test_load_authorized_urls_rejects_non_https_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            url_list = Path(temporary) / "urls.txt"
            url_list.write_text("http://example.com/video.mp4\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "HTTPS direct media URL"):
                load_authorized_urls(url_list)

    def test_download_media_rejects_html_pages(self) -> None:
        response = Mock()
        response.headers = {"Content-Type": "text/html"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("core.transcription.requests.get", return_value=response),
                self.assertRaisesRegex(ValueError, "not a TikTok page link"),
            ):
                download_media("https://www.tiktok.com/@creator/video/123", Path(temporary))

    def test_process_url_list_writes_timestamped_json(self) -> None:
        model = Mock()
        model.transcribe.return_value = (
            iter([SimpleNamespace(start=0.1234, end=1.9876, text=" Xin chào ")]),
            SimpleNamespace(language="vi", duration=2.5),
        )
        response = Mock()
        response.headers = {"Content-Type": "video/mp4", "Content-Length": "3"}
        response.iter_content.return_value = [b"mp4"]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            url_list = root / "urls.txt"
            url_list.write_text("https://cdn.example/video.mp4?token=secret\n", encoding="utf-8")
            with patch("core.transcription.requests.get", return_value=response):
                outputs = process_url_list(
                    url_list,
                    download_dir=root / "downloads",
                    output_dir=root / "transcripts",
                    model=model,
                )

            result = json.loads(outputs[0].read_text(encoding="utf-8"))

        self.assertEqual(result["language"], "vi")
        self.assertEqual(result["duration_seconds"], 2.5)
        self.assertEqual(result["segments"], [{"start": 0.123, "end": 1.988, "text": "Xin chào"}])
        self.assertNotIn("secret", result["source_url"])
        model.transcribe.assert_called_once()


class VadProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        import core.transcription as transcription

        self.transcription = transcription
        transcription._vad_ok = None

    def tearDown(self) -> None:
        self.transcription._vad_ok = None

    def test_native_crash_on_import_disables_vad(self) -> None:
        crashed = SimpleNamespace(returncode=3221225477, stderr=b"")
        with patch("subprocess.run", return_value=crashed) as run, patch.dict("os.environ", {"WHISPER_VAD": ""}):
            ok, reason = self.transcription.probe_onnxruntime()
            self.assertFalse(ok)
            self.assertIn("0xC0000005", reason)
            self.assertFalse(self.transcription.vad_enabled())
            self.assertFalse(self.transcription.vad_enabled())  # probed once, then cached
        self.assertEqual(run.call_count, 2)

    def test_vad_can_be_turned_off_without_probing(self) -> None:
        with patch("subprocess.run") as run, patch.dict("os.environ", {"WHISPER_VAD": "false"}):
            self.assertFalse(self.transcription.vad_enabled())
        run.assert_not_called()

    def test_vad_on_when_onnxruntime_imports(self) -> None:
        fine = SimpleNamespace(returncode=0, stderr=b"")
        with patch("subprocess.run", return_value=fine), patch.dict("os.environ", {"WHISPER_VAD": ""}):
            self.assertTrue(self.transcription.vad_enabled())


if __name__ == "__main__":
    unittest.main()
