from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.tele import approval_bot, system_bot


def _queue() -> list[dict]:
    return [
        {"id": "a1", "idea": {"title": "Khỉ bán gà"}, "publish": False},
        {"id": "b2", "idea": {"title": "Thầy bói <vỉa hè>"}, "publish": True},
    ]


def _query(data: str) -> MagicMock:
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.chat_id = 42
    return query


def _update(query: MagicMock, update_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(callback_query=query, update_id=update_id)


class QueueTelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.queue_file = root / "queue_approved.json"
        self.queue_file.write_text(json.dumps(_queue()), encoding="utf-8")
        self.patches = [
            patch.object(approval_bot, "APPROVED_QUEUE_FILE", self.queue_file),
            patch.object(approval_bot, "get_setting", return_value="42"),
            patch.object(system_bot, "CONTROL_STATE_FILE", root / "state.json"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _callbacks(self, markup) -> list[str]:
        return [button.callback_data for row in markup.inline_keyboard for button in row]

    def test_view_lists_items_with_delete_buttons_and_escapes_titles(self) -> None:
        text, markup = approval_bot._queue_status_view()
        self.assertIn("Thầy bói &lt;vỉa hè&gt;", text)
        callbacks = self._callbacks(markup)
        self.assertEqual(sum(c.startswith("queue_del:") for c in callbacks), 2)
        self.assertIn("queue_clear:ask", callbacks)

    def test_empty_queue_has_no_buttons(self) -> None:
        self.queue_file.write_text("[]", encoding="utf-8")
        text, markup = approval_bot._queue_status_view()
        self.assertIn("trống", text)
        self.assertIsNone(markup)

    def test_delete_one_item(self) -> None:
        _, markup = approval_bot._queue_status_view()
        delete_b2 = [c for c in self._callbacks(markup) if c.startswith("queue_del:")][1]
        query = _query(delete_b2)
        with patch.object(approval_bot, "delete_queue_item", return_value=True) as delete, \
                patch.object(approval_bot.server_control, "worker_lock_busy", return_value=False):
            asyncio.run(approval_bot.handle_approval_callback(_update(query), None))
        delete.assert_called_once_with("b2")
        query.edit_message_text.assert_awaited()

    def test_clear_all_stops_workers_first_and_runs_once(self) -> None:
        calls: list[str] = []
        with patch.object(approval_bot.server_control, "stop_workers",
                          side_effect=lambda: calls.append("stop") or [object()]), \
                patch.object(approval_bot, "clear_approved_queue",
                             side_effect=lambda: calls.append("clear") or 2):
            for _ in range(2):  # second one = the same update re-delivered by Telegram
                query = _query("queue_clear:yes")
                asyncio.run(approval_bot.handle_approval_callback(_update(query, 9), None))
        self.assertEqual(calls, ["stop", "clear"])

    def test_clear_cancel_shows_queue_again(self) -> None:
        query = _query("queue_clear:no")
        with patch.object(approval_bot, "clear_approved_queue") as clear:
            asyncio.run(approval_bot.handle_approval_callback(_update(query), None))
        clear.assert_not_called()
        self.assertIn("Hàng đợi sản xuất", query.edit_message_text.await_args.args[0])


class WorkerSkipsDeletedItemsTests(unittest.TestCase):
    def test_still_queued(self) -> None:
        import main

        with patch.object(main, "read_approved_queue", return_value=[{"id": "a1"}]):
            self.assertTrue(main._still_queued("a1"))
            self.assertFalse(main._still_queued("zz"))


if __name__ == "__main__":
    unittest.main()
