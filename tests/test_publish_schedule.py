from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import core.storage as storage
from core import idea_bank
from core import publish_schedule as ps
from core.gemini import idea_generator
from core.tele import approval_bot, schedule_bot


def _run(coro):
    return asyncio.run(coro)


def _idea(title: str, score: float, scenes: int = 3) -> dict:
    return {
        "title": title,
        "viral_score": score,
        "scene_count_reason": f"{scenes} cảnh",
        "scenes": [
            {"prompt": f"scene {n} prompt", "dialogue": f'@nguoi: "câu {n}"', "duration": "10s"}
            for n in range(1, scenes + 1)
        ],
        "social_posts": {"youtube": {"title": f"{title} #Shorts", "caption": "cap", "hashtags": "#haihuoc"}},
    }


class StorageIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.drafts = root / "drafts.json"
        self.queue = root / "queue.json"
        self.drafts.write_text("[]", encoding="utf-8")
        self.queue.write_text("[]", encoding="utf-8")
        self.patches = [
            patch.object(ps, "CONFIG_FILE", root / "publish_schedule.json"),
            patch.object(ps, "STORAGE_DIR", root),
            patch.object(idea_bank, "BANK_FILE", root / "idea_bank.json"),
            patch.object(idea_bank, "STORAGE_DIR", root),
            patch.object(idea_bank, "refill_from_trends", return_value=0),
            patch.object(storage, "DRAFTS_FILE", self.drafts),
            patch.object(storage, "APPROVED_QUEUE_FILE", self.queue),
            patch.object(storage, "QUEUE_LOCK_FILE", root / "queue.lock"),
            patch.object(storage, "ensure_storage", lambda: None),
            patch.object(approval_bot, "DRAFTS_FILE", self.drafts),
            patch("core.config.get_setting", return_value="42"),
        ]
        for p in self.patches:
            p.start()
        schedule_bot._running.clear()
        schedule_bot._waiting.clear()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class ScheduleConfigTests(StorageIsolation):
    def test_parse_slots(self) -> None:
        self.assertEqual(ps.parse_slots("11:30, 19h 7.05 21 25:00 abc"), ["11:30", "19:00", "07:05", "21:00"])

    def test_ideas_are_due_lead_hours_before_each_slot_once(self) -> None:
        ps.update_config(enabled=True, slots=["19:00", "11:30"], lead_hours=3)
        config = ps.load_config()
        self.assertEqual(config["slots"], ["11:30", "19:00"])
        self.assertEqual(ps.due_posts(config, datetime(2026, 11, 21, 15, 59)), [])
        due = ps.due_posts(config, datetime(2026, 11, 21, 16, 0))
        self.assertEqual(due, [datetime(2026, 11, 21, 19, 0)])
        ps.mark_generated(due[0])
        self.assertEqual(ps.due_posts(ps.load_config(), datetime(2026, 11, 21, 16, 5)), [])
        # disabled -> nothing
        ps.update_config(enabled=False)
        self.assertEqual(ps.due_posts(ps.load_config(), datetime(2026, 11, 22, 16, 0)), [])

    def test_youtube_publish_at_is_none_when_the_slot_passed(self) -> None:
        now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
        future = {"publish_at": (now + timedelta(hours=3)).isoformat()}
        self.assertEqual(ps.youtube_publish_at(future, now), now + timedelta(hours=3))
        self.assertIsNone(ps.youtube_publish_at({"publish_at": (now - timedelta(minutes=5)).isoformat()}, now))

    def test_pick_best_uses_viral_score_and_ignores_skipped(self) -> None:
        drafts = [
            {"id": "a", "state": "pending", "idea": _idea("A", 7)},
            {"id": "b", "state": "skipped", "idea": _idea("B", 9.5)},
            {"id": "c", "state": "pending", "idea": _idea("C", 8.2)},
        ]
        self.assertEqual(ps.pick_best(drafts)["id"], "c")
        self.assertIsNone(ps.pick_best([{"id": "b", "state": "skipped", "idea": _idea("B", 9)}]))

    def test_publish_scheduled_sends_youtube_only_with_publish_at(self) -> None:
        publish_at = (datetime.now() + timedelta(hours=2)).astimezone()
        item = {"id": "x", "idea": _idea("Khỉ bán vàng", 8), "schedule": {
            "publish_at": publish_at.isoformat(), "remove_logo": True, "platforms": ["youtube"]}}
        with patch.object(ps, "prepare_video", return_value=Path("clean.mp4")), \
                patch("core.social_uploader.publish_to_social",
                      return_value={"youtube": {"success": True, "url": "https://youtu.be/x"}}) as publish:
            status = ps.publish_scheduled(item, Path("raw.mp4"))
        configs = publish.call_args.kwargs["platform_configs"]
        self.assertEqual(publish.call_args.args[0], "clean.mp4")
        self.assertTrue(configs["youtube"]["enabled"])
        self.assertFalse(configs["tiktok"]["enabled"] or configs["facebook"]["enabled"])
        self.assertEqual(configs["youtube"]["title"], "Khỉ bán vàng #Shorts")
        self.assertAlmostEqual(configs["youtube"]["publish_at"].timestamp(), publish_at.timestamp(), delta=1)
        self.assertTrue(status["success"])
        self.assertEqual(status["video"], "clean.mp4")


class ScheduleBotTests(StorageIsolation):
    def _bot(self) -> MagicMock:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        return bot

    def _generate(self, bot, now) -> None:
        async def scenario() -> None:
            await schedule_bot.tick(bot, now)
            await asyncio.gather(*schedule_bot._background_tasks)

        with patch("core.gemini.idea_generator.generate_skill_ideas",
                   return_value=[_idea("Ý A", 7.5), _idea("Ý B", 9.0)]):
            _run(scenario())

    def test_tick_sends_ideas_with_schedule_then_manual_approval_queues_it(self) -> None:
        ps.update_config(enabled=True, slots=["19:00"], lead_hours=3, ideas_per_slot=2)
        bot = self._bot()
        self._generate(bot, datetime(2026, 11, 21, 16, 0))

        self.assertEqual(bot.send_message.await_count, 2)
        drafts = json.loads(self.drafts.read_text(encoding="utf-8"))
        self.assertEqual([d["source"] for d in drafts], ["schedule", "schedule"])
        self.assertTrue(drafts[0]["schedule"]["publish_at"].startswith("2026-11-21T19:00"))
        markup = bot.send_message.await_args_list[0].kwargs["reply_markup"]
        ok = markup.inline_keyboard[0][0]
        self.assertTrue(ok.text.startswith("🗓 Duyệt → đăng 19:00"))

        query = MagicMock()
        query.data = ok.callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        with patch.object(approval_bot, "_auto_start_video_worker_if_needed") as start:
            _run(schedule_bot.handle_callback(query))
        start.assert_called_once_with(publish=False)
        queue = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(queue[0]["schedule"]["publish_at"], drafts[0]["schedule"]["publish_at"])
        self.assertFalse(queue[0]["publish"])
        batch = next(iter(ps.load_config()["batches"].values()))
        self.assertEqual(batch["approved"], "manual")

        # a manual approval means no auto-approval later
        with patch.object(approval_bot, "_auto_start_video_worker_if_needed"):
            self.assertEqual(_run(schedule_bot.auto_approve_overdue(bot, datetime.now() + timedelta(hours=2))), 0)

    def test_slot_takes_topic_from_idea_bank_and_alt_keeps_it(self) -> None:
        ps.update_config(enabled=True, slots=["19:00"], lead_hours=3, ideas_per_slot=1)
        idea_bank.add("trend ABC", "trend")
        mine, _ = idea_bank.add("Lương không tăng mà giá tăng là đang được giảm giá chính mình", "user")
        bot = self._bot()
        with patch("core.gemini.idea_generator.generate_skill_ideas",
                   return_value=[_idea("Ý A", 8)]) as generate:
            async def scenario() -> None:
                await schedule_bot.tick(bot, datetime(2026, 11, 21, 16, 0))
                await asyncio.gather(*schedule_bot._background_tasks)
                # 🔄 Thêm ý khác for the same slot → same topic, no second bank idea burned
                await schedule_bot.generate_for_slot(bot, datetime(2026, 11, 21, 19, 0), count=1)

            _run(scenario())
        topics = [c.args[0] for c in generate.call_args_list]
        self.assertEqual(topics, [mine["text"], mine["text"]])  # the user's idea beats the trend
        self.assertEqual([e["text"] for e in idea_bank.unused()], ["trend ABC"])
        self.assertIn("Kho ý tưởng", bot.send_message.await_args_list[0].kwargs["text"])

    def test_empty_bank_falls_back_to_weekday_theme(self) -> None:
        topic = idea_bank.choose_topic(datetime(2026, 11, 25, 19, 0))  # a Wednesday
        self.assertEqual(topic["source"], "weekday")
        self.assertIn("tục ngữ", topic["topic"])
        self.assertIsNone(topic["entry_id"])
        fixed = idea_bank.choose_topic(datetime(2026, 11, 25, 19, 0), "giá vàng")
        self.assertEqual((fixed["source"], fixed["topic"]), ("config", "giá vàng"))

    def test_unapproved_slot_auto_approves_best_idea_after_an_hour(self) -> None:
        ps.update_config(enabled=True, slots=["19:00"], lead_hours=3, auto_approve_minutes=60)
        bot = self._bot()
        self._generate(bot, datetime(2026, 11, 21, 16, 0))
        sent_at = datetime.fromisoformat(next(iter(ps.load_config()["batches"].values()))["sent_at"])

        with patch.object(approval_bot, "_auto_start_video_worker_if_needed") as start:
            self.assertEqual(_run(schedule_bot.auto_approve_overdue(bot, sent_at + timedelta(minutes=59))), 0)
            self.assertEqual(_run(schedule_bot.auto_approve_overdue(bot, sent_at + timedelta(minutes=60))), 1)
        start.assert_called_once_with(publish=False)
        queue = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual([q["idea"]["title"] for q in queue], ["Ý B"])  # score 9.0 beats 7.5
        self.assertIn("tự duyệt", bot.send_message.await_args.kwargs["text"])
        self.assertEqual(next(iter(ps.load_config()["batches"].values()))["approved"], "auto")

    def test_auto_approve_off_never_approves(self) -> None:
        ps.update_config(enabled=True, slots=["19:00"], lead_hours=3, auto_approve_minutes=0)
        bot = self._bot()
        self._generate(bot, datetime(2026, 11, 21, 16, 0))
        with patch.object(approval_bot, "_auto_start_video_worker_if_needed"):
            self.assertEqual(_run(schedule_bot.auto_approve_overdue(bot, datetime.now() + timedelta(days=1))), 0)

    def test_typed_times_are_added_to_the_config(self) -> None:
        message = MagicMock(chat_id=42)
        message.reply_text = AsyncMock()
        schedule_bot._waiting["42"] = "slots"
        self.assertTrue(_run(schedule_bot.handle_text(message, "19:00 11h30")))
        self.assertEqual(ps.load_config()["slots"], ["11:30", "19:00"])
        self.assertFalse(schedule_bot.is_waiting(42))
        self.assertFalse(_run(schedule_bot.handle_text(message, "19:00")))  # not waiting anymore


class SkillIdeaGeneratorTests(unittest.TestCase):
    def test_skill_drives_prompt_and_output_shape(self) -> None:
        reply = json.dumps([{
            "title": "Lương và giá",
            "viral_score": 8.5,
            "caption": "Khỉ nói đúng 🔧😂",
            "hashtags": "#haihuoc #reelsfb",
            "engagement_prompt": "Khỉ nói sai chỗ nào?",
            "youtube_title": "Lương không tăng",
            "scenes": [{"prompt": f"p{n}", "dialogue": f"d{n}"} for n in range(3)],
        }])
        with patch.object(idea_generator, "_call_gemini_json", return_value=reply) as call, \
                patch.object(idea_generator, "_load_recent_draft_titles", return_value=[]):
            ideas = idea_generator.generate_skill_ideas("giá vàng", 1)
        prompt = call.call_args.args[0]
        self.assertIn("VIDEO-HÀI-KHỈ-NGƯỜI v16.1", prompt)  # the SKILL.md content is sent
        self.assertIn("TOPIC / INPUT: giá vàng", prompt)
        idea = ideas[0]
        self.assertEqual(idea["duration_seconds"], 30)
        self.assertIn("ĐÚNG 3 cảnh", prompt)
        self.assertEqual(idea["scenes"][-1]["type"], "punchline")
        self.assertEqual(idea["scenes"][0]["duration"], "10s")
        self.assertEqual(idea["viral_score"], 8.5)
        youtube = idea["social_posts"]["youtube"]
        self.assertIn("Khỉ nói sai chỗ nào?", youtube["caption"])
        self.assertIn("#Shorts", youtube["hashtags"])

    def test_scene_count_other_than_3_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            idea_generator._validate_skill_ideas(
                [{"title": "x", "scenes": [{"prompt": "p", "dialogue": "d"}] * 2}], 1
            )
        with self.assertRaises(ValueError):
            idea_generator._validate_skill_ideas(
                [{"title": "x", "scenes": [{"prompt": "p", "dialogue": "d"}] * 4}], 1
            )


if __name__ == "__main__":
    unittest.main()
