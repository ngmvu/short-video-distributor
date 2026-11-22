from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core import idea_bank
from core.link_transcriber import TranscriptResult, VideoInfo
from core.tele import idea_bank_bot
from core.trend_radar import TrendSignal


def _signal(title: str, fit: int) -> TrendSignal:
    return TrendSignal(title, "1000+", "", "", (), 1, fit, "")


class IdeaBankTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(idea_bank, "BANK_FILE", root / "idea_bank.json"),
            patch.object(idea_bank, "STORAGE_DIR", root),
            patch("core.storage.ensure_storage", lambda: None),
        ]
        for p in self.patches:
            p.start()
        idea_bank_bot._waiting.clear()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_order_user_then_link_then_trend_and_duplicates(self) -> None:
        idea_bank.add("trend 1", "trend")
        idea_bank.add("tình huống từ video", "link", {"url": "https://vt.tiktok.com/x/"})
        idea_bank.add("Ý của tôi", "user")
        _, created = idea_bank.add("  ý CỦA toi!! ", "user")  # same text, other case/accents
        self.assertFalse(created)
        _, created = idea_bank.add("khác chữ", "link", {"url": "https://vt.tiktok.com/x/"})
        self.assertFalse(created)  # same video link
        self.assertEqual([e["source"] for e in idea_bank.unused()], ["user", "link", "trend"])

        first = idea_bank.unused()[0]
        idea_bank.mark_used(first["id"], "2026-11-22 19:00")
        self.assertEqual(idea_bank.stats()["total"], 2)
        self.assertTrue(idea_bank.remove(idea_bank.unused()[0]["id"]))
        self.assertEqual([e["source"] for e in idea_bank.unused()], ["trend"])

    def test_choose_topic_refills_one_fitting_trend_when_empty(self) -> None:
        with patch("core.trend_radar.get_trend_signals",
                   return_value=[_signal("Bóng đá U23", 0), _signal("Giá xăng tăng", 25)]):
            topic = idea_bank.choose_topic(__import__("datetime").datetime(2026, 11, 23, 19))
        self.assertEqual(topic["source"], "trend")
        self.assertIn("Giá xăng tăng", topic["topic"])
        self.assertNotIn("U23", topic["topic"])

    def test_seasonal_hints(self) -> None:
        self.assertIn("Tết", idea_bank.seasonal_hint(date(2027, 1, 25)))
        self.assertIn("Black Friday", idea_bank.seasonal_hint(date(2026, 11, 25)))  # BF 2026 = 27/11
        self.assertIn("Noel", idea_bank.seasonal_hint(date(2026, 12, 22)))
        self.assertEqual(idea_bank.seasonal_hint(date(2026, 7, 15)), "")

    def test_bot_adds_typed_idea_and_link_situation(self) -> None:
        message = MagicMock()
        message.chat_id = 42
        message.reply_text = AsyncMock()

        asyncio.run(idea_bank_bot.handle_command(message, ["Có", "tiền", "thì", "sợ", "mất"]))
        self.assertEqual(idea_bank.unused()[0]["text"], "Có tiền thì sợ mất")

        # not in add mode → ordinary chat text is left to other handlers
        self.assertFalse(asyncio.run(idea_bank_bot.handle_text(message, "xin chào")))

        result = TranscriptResult(
            video=VideoInfo("tiktok", "https://vt.tiktok.com/abc/", title="Video hot", view_count=5),
            segments=[{"text": "câu một"}, {"text": "câu hai"}],
        )

        async def scenario() -> None:
            idea_bank_bot._waiting.add("42")
            self.assertTrue(await idea_bank_bot.handle_text(message, "https://vt.tiktok.com/abc/"))
            await asyncio.gather(*idea_bank_bot._background_tasks)

        with patch("core.link_transcriber.transcribe_links", return_value=[result]), \
                patch("core.gemini.idea_generator.extract_situation_from_video",
                      return_value="Chú khoe xe mới, khỉ hỏi giá") as extract:
            asyncio.run(scenario())
        extract.assert_called_once_with("Video hot", "câu một câu hai")
        link_entry = [e for e in idea_bank.unused() if e["source"] == "link"][0]
        self.assertEqual(link_entry["text"], "Chú khoe xe mới, khỉ hỏi giá")
        self.assertEqual(link_entry["meta"]["url"], "https://vt.tiktok.com/abc/")


if __name__ == "__main__":
    unittest.main()
