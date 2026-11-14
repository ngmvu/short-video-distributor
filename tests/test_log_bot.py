from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.tele import log_bot

SAMPLE = """2026-11-14 16:47:21,098 INFO short_video_distributor.thoai_hot: B\\u00f3c tho\\u1ea1i 1/1: https://vt.tiktok.com/x/
2026-11-14 16:47:22,000 INFO httpx: HTTP Request: POST https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ/getUpdates
2026-11-14 16:47:24,000 INFO faster_whisper: VAD filter removed 00:09.154 of audio
[download]   1.0% of 1MiB\r[download] 100% of 1MiB
2026-11-14 16:47:30,000 ERROR telegram.ext.Updater: Exception happened while polling for updates.
Traceback (most recent call last):
  File "x.py", line 1, in <module>
telegram.error.Conflict: terminated by other getUpdates request
2026-11-14 16:47:31,000 WARNING short_video_distributor.thoai_hot: Bỏ qua https://y: blocked
"""


class LogBotTests(unittest.TestCase):
    def _render(self, view: str) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "dashboard.log"
            log.write_text(SAMPLE, encoding="utf-8")
            with (
                patch.object(log_bot, "LOG_FILE", log),
                patch.object(log_bot, "LOG_BACKUP_FILE", Path(temporary) / "none"),
            ):
                return log_bot.render_view(view)

    def test_crawl_view_only_shows_crawl_and_whisper_lines(self) -> None:
        text = self._render("crawl")
        self.assertIn("Bóc thoại 1/1", text)  # \u escapes decoded
        self.assertIn("VAD filter removed", text)
        self.assertIn("Bỏ qua https://y", text)
        self.assertNotIn("getUpdates", text)

    def test_errors_view_collapses_traceback(self) -> None:
        text = self._render("errors")
        self.assertIn("Conflict: terminated by other getUpdates request", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("Bóc thoại", text)

    def test_recent_view_drops_http_noise_and_progress_bars(self) -> None:
        text = self._render("recent")
        self.assertNotIn("HTTP Request", text)
        self.assertNotIn("1.0% of", text)
        self.assertIn("16:47:24 INFO VAD", text)

    def test_redacts_tokens(self) -> None:
        with patch.dict(os.environ, {"MY_API_KEY": "supersecretvalue"}):
            text = log_bot.redact(
                "key supersecretvalue bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ ?access_token=abc"
            )
        self.assertNotIn("supersecretvalue", text)
        self.assertNotIn("ABCDEFGHIJ", text)
        self.assertNotIn("=abc", text)


if __name__ == "__main__":
    unittest.main()
