from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.tele import transcribe_bot


def _message(chat_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(chat_id=chat_id, reply_text=AsyncMock())


class TranscribeBotTests(unittest.TestCase):
    def setUp(self) -> None:
        transcribe_bot._waiting.clear()

    def test_menu_opens_link_mode_and_pasted_link_runs(self) -> None:
        message = _message()
        asyncio.run(transcribe_bot.start_link_mode(message))
        self.assertTrue(transcribe_bot.is_waiting(42))
        with patch.object(transcribe_bot, "_start_job", new=AsyncMock()) as start:
            handled = asyncio.run(
                transcribe_bot.handle_text(message, "https://vt.tiktok.com/ZSp4Stubm/")
            )
        self.assertTrue(handled)
        self.assertEqual(start.call_args.args[1], ["https://vt.tiktok.com/ZSp4Stubm/"])
        self.assertTrue(transcribe_bot.is_waiting(42), "stays in link mode for more links")

    def test_link_mode_rejects_text_without_video_link(self) -> None:
        message = _message()
        asyncio.run(transcribe_bot.start_link_mode(message))
        with patch.object(transcribe_bot, "_start_job", new=AsyncMock()) as start:
            handled = asyncio.run(transcribe_bot.handle_text(message, "khỉ hài"))
        self.assertTrue(handled)
        start.assert_not_called()
        self.assertIn("Không thấy link", message.reply_text.call_args.args[0])

    def test_pasted_link_is_detected_without_menu(self) -> None:
        with patch.object(transcribe_bot, "_start_job", new=AsyncMock()) as start:
            handled = asyncio.run(
                transcribe_bot.handle_text(_message(), "https://www.tiktok.com/@a/video/1")
            )
        self.assertTrue(handled)
        start.assert_called_once()

    def test_normal_text_is_left_for_other_handlers(self) -> None:
        self.assertFalse(asyncio.run(transcribe_bot.handle_text(_message(), "vợ giấu quỹ đen")))
        self.assertFalse(
            asyncio.run(transcribe_bot.handle_text(_message(), "https://example.com/page"))
        )

    def test_done_button_leaves_link_mode(self) -> None:
        message = _message()
        asyncio.run(transcribe_bot.start_link_mode(message))
        query = SimpleNamespace(
            data="bocthoai:done",
            message=message,
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        asyncio.run(transcribe_bot.handle_callback(query))
        self.assertFalse(transcribe_bot.is_waiting(42))


if __name__ == "__main__":
    unittest.main()
