from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.tele import import_bot


def _run(coro):
    return asyncio.run(coro)


def _buttons(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class ImportBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.storage = root / "storage"
        self.downloads = self.storage / "downloads"
        self.downloads.mkdir(parents=True)
        self.outside = root / "Videos"
        self.outside.mkdir()
        self.patches = [
            patch.object(import_bot, "STORAGE_DIR", self.storage),
            patch.object(import_bot, "DOWNLOADS_DIR", self.downloads),
            patch.object(import_bot, "STATE_FILE", self.storage / "telegram_imports.json"),
            patch.object(import_bot, "save_video_social_metadata"),
            patch.object(import_bot, "save_publish_report"),
            patch.object(import_bot, "send_video_file", AsyncMock()),
        ]
        self.mocks = [p.start() for p in self.patches]
        import_bot._waiting.clear()
        import_bot._busy.clear()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _message(self) -> SimpleNamespace:
        status = MagicMock(edit_text=AsyncMock())
        return SimpleNamespace(
            chat_id=7, reply_text=AsyncMock(return_value=status), status=status,
            get_bot=MagicMock(return_value="bot"),
        )

    def _query(self, data: str, message=None) -> MagicMock:
        query = MagicMock()
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        query.message = message or self._message()
        query.message.edit_text = AsyncMock()
        return query

    def _imported(self, name: str = "clip.mp4") -> str:
        (self.downloads / name).write_bytes(b"video")
        return import_bot.register(self.downloads / name)

    def test_sent_video_is_downloaded_into_downloads_with_action_buttons(self) -> None:
        async def download_to_drive(custom_path):
            Path(custom_path).write_bytes(b"video-bytes")

        tg_file = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download_to_drive))
        video = SimpleNamespace(file_size=1000, file_name="My clip?.mp4", get_file=AsyncMock(return_value=tg_file))
        message = self._message()
        message.video = video

        self.assertTrue(_run(import_bot.handle_video(message)))

        self.assertTrue((self.downloads / "My clip.mp4").is_file())
        text = message.status.edit_text.await_args.args[0]
        self.assertIn("Đã import", text)
        markup = message.status.edit_text.await_args.kwargs["reply_markup"]
        token = _buttons(markup)[0].split(":")[-1]
        self.assertEqual(import_bot._get(token)["video"], "My clip.mp4")

    def test_text_message_is_not_a_video(self) -> None:
        message = self._message()
        message.video = None
        message.document = None
        self.assertFalse(_run(import_bot.handle_video(message)))

    def test_pasted_path_is_copied_only_in_import_mode(self) -> None:
        source = self.outside / "ngoai.mp4"
        source.write_bytes(b"video")
        message = self._message()
        self.assertFalse(_run(import_bot.handle_text(message, str(source))))

        _run(import_bot.start_mode(message))
        self.assertTrue(_run(import_bot.handle_text(message, f'"{source}"')))
        self.assertTrue((self.downloads / "ngoai.mp4").is_file())
        self.assertTrue(source.is_file())  # copied, not moved

    def test_file_already_in_downloads_is_not_copied(self) -> None:
        (self.downloads / "a.mp4").write_bytes(b"video")
        message = self._message()
        _run(import_bot.handle_command(message, ["a.mp4"]))
        self.assertEqual(sorted(p.name for p in self.downloads.iterdir()), ["a.mp4"])
        # The imported video itself is sent back, carrying the action buttons.
        send = self.mocks[5]
        path, caption, _bot, chat_id, markup = send.await_args.args
        self.assertEqual((Path(path).resolve(), chat_id), ((self.downloads / "a.mp4").resolve(), 7))
        self.assertIn("Đã import", caption)
        self.assertTrue(_buttons(markup)[0].startswith("imp:logo:"))

    def test_caption_then_toggle_then_publish(self) -> None:
        token = self._imported()
        generated = {
            "general_title": "Khỉ sửa xe",
            "tiktok": {"title": "Hook", "caption": "Cap TT", "hashtags": "#xuhuong #haihuoc"},
            "facebook": {"title": "FB", "caption": "Cap FB", "hashtags": "#reelsvn"},
            "youtube": {"title": "Khỉ sửa xe", "caption": "Cap YT", "hashtags": "#Shorts #haihuoc"},
        }
        cap_query = self._query(f"imp:cap:{token}")
        with patch("core.gemini.idea_generator.generate_social_posts_for_video", return_value=generated):
            async def caption() -> None:
                await import_bot.handle_callback(cap_query)
                await asyncio.gather(*import_bot._background_tasks)
            _run(caption())

        entry = import_bot._get(token)
        self.assertEqual(entry["title"], "Khỉ sửa xe")
        self.assertEqual(entry["social_posts"]["tiktok"]["caption"], "Cap TT")
        preview = cap_query.message.reply_text.return_value.edit_text.await_args
        self.assertIn("Cap FB", preview.args[0])
        self.assertIn(f"imp:pub:{token}", _buttons(preview.kwargs["reply_markup"]))

        _run(import_bot.handle_callback(self._query(f"imp:t_facebook:{token}")))
        self.assertEqual(import_bot._get(token)["platforms"], ["tiktok", "youtube"])

        statuses = {
            "tiktok": {"success": True, "url": None},
            "youtube": {"success": False, "error": "quota"},
            "facebook": {"success": False, "skipped": True},
        }
        pub_query = self._query(f"imp:pub:{token}")
        with patch("core.social_uploader.publish_to_social", return_value=statuses) as publish:
            async def publish_now() -> None:
                await import_bot.handle_callback(pub_query)
                await asyncio.gather(*import_bot._background_tasks)
            _run(publish_now())

        configs = publish.call_args.kwargs["platform_configs"]
        self.assertEqual(publish.call_args.args[0], str(self.downloads / "clip.mp4"))
        self.assertEqual([k for k, c in configs.items() if c["enabled"]], ["tiktok", "youtube"])
        self.assertEqual(configs["tiktok"]["hashtags"], ["xuhuong", "haihuoc"])
        self.assertIn("#Shorts", configs["youtube"]["title"])
        result = pub_query.message.reply_text.return_value.edit_text.await_args.args[0]
        self.assertIn("✅ 🎵 TikTok", result)
        self.assertIn("❌ ▶️ YouTube: quota", result)
        self.assertNotIn("Facebook", result)
        self.assertTrue(import_bot._get(token)["published"])
        self.assertEqual(self.mocks[4].call_args.args[0]["source"], "telegram")

    def test_publish_needs_captions_first(self) -> None:
        token = self._imported()
        query = self._query(f"imp:pub:{token}")
        with patch("core.social_uploader.publish_to_social") as publish:
            _run(import_bot.handle_callback(query))
        publish.assert_not_called()
        self.assertTrue(query.answer.await_args.kwargs.get("show_alert"))

    def test_double_tap_while_busy_is_ignored(self) -> None:
        token = self._imported()
        import_bot._busy.add(token)
        query = self._query(f"imp:cap:{token}")
        _run(import_bot.handle_callback(query))
        self.assertIn("Đang xử lý", query.answer.await_args.args[0])
        self.assertFalse(import_bot._background_tasks)

    def test_logo_button_runs_removal_and_offers_buttons_for_the_clean_copy(self) -> None:
        token = self._imported()
        query = self._query(f"imp:logo:{token}")
        with patch.object(import_bot.watermark_bot, "start_job") as start_job:
            _run(import_bot.handle_callback(query))
        kwargs = start_job.call_args.kwargs
        self.assertEqual(kwargs["local_path"], self.downloads / "clip.mp4")
        cleaned = self.downloads / "clip-nologo.mp4"
        cleaned.write_bytes(b"clean")
        new_token = _buttons(kwargs["markup_for"](cleaned))[0].split(":")[-1]
        self.assertEqual(import_bot._get(new_token)["video"], "clip-nologo.mp4")

    def test_flow_scene_videos_get_logo_button_and_full_video_also_publish(self) -> None:
        (self.downloads / "x-scene1.mp4").write_bytes(b"v")
        (self.downloads / "x-full.mp4").write_bytes(b"v")
        scene = _buttons(import_bot.flow_video_markup(self.downloads / "x-scene1.mp4"))
        full = _buttons(import_bot.flow_video_markup(self.downloads / "x-full.mp4", with_publish=True))
        self.assertEqual(len(scene), 1)
        self.assertTrue(scene[0].startswith("imp:logo:"))
        self.assertEqual([b.split(":")[1] for b in full], ["logo", "cap"])
        self.assertEqual(import_bot._get(scene[0].split(":")[-1])["video"], "x-scene1.mp4")
        self.assertIsNone(import_bot.flow_video_markup(self.outside / "missing.mp4"))

    def test_notifier_sends_each_scene_with_logo_button(self) -> None:
        from core.tele import notifier

        clips = []
        for n in (1, 2):
            clip = self.downloads / f"x-scene{n}.mp4"
            clip.write_bytes(b"v")
            clips.append(clip)
        full = self.downloads / "x-full.mp4"
        full.write_bytes(b"v")
        with (
            patch.object(notifier, "get_setting", return_value="1"),
            patch.object(notifier, "_send_single_video_file", AsyncMock()) as send,
            patch.object(notifier, "_send_scene_failure_notice", AsyncMock()),
            patch.object(notifier.asyncio, "sleep", AsyncMock()),
        ):
            _run(notifier.send_video_to_telegram(str(full), "Khỉ", MagicMock(), scene_clips=clips))
        markups = [call.args[5] for call in send.await_args_list]
        self.assertEqual([len(_buttons(m)) for m in markups], [1, 1, 2])
        self.assertTrue(all(_buttons(m)[0].startswith("imp:logo:") for m in markups))

    def test_missing_video_or_unknown_token_alerts(self) -> None:
        query = self._query("imp:cap:deadbeef00")
        _run(import_bot.handle_callback(query))
        self.assertTrue(query.answer.await_args.kwargs.get("show_alert"))


if __name__ == "__main__":
    unittest.main()
