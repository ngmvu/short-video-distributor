from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.gemini.idea_generator import (
    _candidate_api_keys,
    _normalize_social_posts,
    _validate_ideas,
    format_flow_agent_prompt,
    generate_ideas,
    generate_social_posts_for_video,
    suggest_trending_topics,
)
from core.trend_radar import TrendRadarError, TrendSignal


def _trend_signal(
    title: str, rank: int, fit_score: int = 25
) -> TrendSignal:
    return TrendSignal(
        title=title,
        approximate_traffic="1000+",
        published_at="Sat, 14 Nov 2026 00:00:00 +0000",
        source_url="https://trends.google.com/trending/rss?geo=VN",
        related_sources=(),
        source_rank=rank,
        channel_fit_score=fit_score,
        fetched_at="2026-11-14T00:00:00+00:00",
    )


class IdeaGeneratorTests(unittest.TestCase):
    def test_flow_agent_prompt_requires_silent_listener_to_react(self) -> None:
        idea = {
            "title": "Chú khách hay quên",
            "scenes": [
                {
                    "first_frame": "@nguoi + @khi + @Voice_Nguoi",
                    "prompt": "The uncle asks a question.",
                    "voice": "@Voice_Nguoi",
                    "dialogue": "Xe tôi đâu?",
                }
            ],
        }

        prompt = format_flow_agent_prompt(idea)

        self.assertTrue(prompt.startswith("Create exactly 1 separate video clips"))
        self.assertIn("Generate all 1 scenes; do not stop after the first.", prompt)
        self.assertIn("Scene 1 — Input / First Frame:", prompt)
        self.assertNotIn("Select the saved Character assets", prompt)
        self.assertIn("Input / First Frame: @nguoi + @khi + @Voice_Nguoi", prompt)
        self.assertNotIn("QUY TẮC DIỄN XUẤT:", prompt)
        self.assertIn("PERFORMANCE: @nguoi is the only speaker", prompt)
        self.assertIn("@khi stays silent with a relaxed closed mouth", prompt)
        self.assertIn("never freeze or hold a static pose", prompt)

    def test_flow_agent_prompt_requests_all_scenes_in_one_submission(self) -> None:
        idea = {
            "scenes": [
                {"first_frame": "@nguoi + @khi", "prompt": "The uncle teaches a lesson."},
                {"first_frame": "@khi + @nguoi", "prompt": "The monkey grinds a wrench."},
                {"first_frame": "@nguoi + @khi", "prompt": "The uncle reacts to the wrench."},
            ]
        }

        prompt = format_flow_agent_prompt(idea)

        self.assertTrue(prompt.startswith("Create exactly 3 separate video clips"))
        self.assertIn("Generate all 3 scenes; do not stop after the first.", prompt)
        for scene_number, scene in enumerate(idea["scenes"], start=1):
            self.assertIn(f"Scene {scene_number} — Input / First Frame:", prompt)
            self.assertIn(scene["prompt"], prompt)

    def test_suggest_trending_topics_returns_sourced_gemini_topics(self) -> None:
        response = json.dumps(
            [
                {"trend_index": 1, "premise": "Khỉ mở quán bán cà phê bằng AI"},
                {"trend_index": 2, "premise": "Ông chú đặt vé xe nhưng quên ngày"},
            ],
            ensure_ascii=False,
        )
        with (
            patch(
                "core.gemini.idea_generator.get_trend_signals",
                return_value=[
                    _trend_signal("AI", 3, 50),
                    _trend_signal("vé xe", 8, 25),
                ],
            ),
            patch(
                "core.gemini.idea_generator._call_gemini_json",
                return_value=response,
            ) as call_gemini,
        ):
            topics = suggest_trending_topics(2)

        self.assertIn("Khỉ mở quán bán cà phê bằng AI", topics[0])
        self.assertIn("Nguồn tham khảo: Google Trends Việt Nam", topics[0])
        self.assertIn("từ khóa: AI", topics[0])
        self.assertIn("cập nhật: 2026-11-14T00:00:00+00:00", topics[0])
        self.assertIn("Ông chú đặt vé xe nhưng quên ngày", topics[1])
        call_gemini.assert_called_once()
        self.assertIn("tình huống cụ thể", call_gemini.call_args.args[0])
        self.assertIn("bộ đôi cố định", call_gemini.call_args.args[0])
        self.assertIn("khỉ ma ranh, chủ tiệm sửa xe", call_gemini.call_args.args[0])
        self.assertIn("hư cấu, ẩn danh", call_gemini.call_args.args[0])
        self.assertIn("TÍN HIỆU GOOGLE TRENDS VIỆT NAM", call_gemini.call_args.args[0])
        self.assertNotIn("chứng khoán", call_gemini.call_args.args[0])

    def test_suggest_trending_topics_rejects_missing_or_duplicate_topics(self) -> None:
        for response in (
            '[{"trend_index": 1, "premise": "Chủ đề A"}]',
            (
                '[{"trend_index": 1, "premise": "Chủ đề A"}, '
                '{"trend_index": 1, "premise": "Chủ đề B"}]'
            ),
            (
                '[{"trend_index": 1, "premise": "Chủ đề A"}, '
                '{"trend_index": 2, "premise": "Chủ đề A"}]'
            ),
        ):
            with (
                self.subTest(response=response),
                patch(
                    "core.gemini.idea_generator.get_trend_signals",
                    return_value=[
                        _trend_signal("AI", 1),
                        _trend_signal("vé xe", 2),
                    ],
                ),
                patch(
                    "core.gemini.idea_generator._call_gemini_json",
                    return_value=response,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Gemini could not turn Google Trends signals"
                ):
                    suggest_trending_topics(2)

    def test_suggest_trending_topics_surfaces_gemini_failure(self) -> None:
        with (
            patch(
                "core.gemini.idea_generator.get_trend_signals",
                return_value=[_trend_signal("AI", 1)],
            ),
            patch(
                "core.gemini.idea_generator._call_gemini_json",
                side_effect=RuntimeError("API unavailable"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Gemini could not turn Google Trends signals"
            ):
                suggest_trending_topics(1)

    def test_suggest_trending_topics_reports_missing_trend_data(self) -> None:
        with patch(
            "core.gemini.idea_generator.get_trend_signals",
            side_effect=TrendRadarError("offline"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Google Trends Việt Nam chưa cung cấp"
            ):
                suggest_trending_topics(1)

    def test_generate_ideas_uses_configured_genai_model_and_json_mode(self) -> None:
        ideas = [
            {
                "title": f"Idea {index}",
                "character_1": {"ingredient": "@khi_tho", "image_prompt": "Rabbit"},
                "character_2": {"ingredient": "@khach", "image_prompt": "Customer"},
                "scenes": [
                    {"duration": "3s", "prompt": "Intro", "type": "intro"},
                    {
                        "duration": "3-4s",
                        "prompt": "Dialogue",
                        "dialogue": "Xin chào",
                        "voice": "@Voice_Khach",
                        "type": "dialogue",
                    },
                    {
                        "duration": "5-7s",
                        "prompt": "Punchline",
                        "dialogue": "Bất ngờ!",
                        "voice": "@Voice_Khi",
                        "type": "punchline",
                    },
                ],
            }
            for index in (1, 2)
        ]
        generate_content = Mock(return_value=SimpleNamespace(text=json.dumps(ideas)))
        client = Mock()
        client.models.generate_content = generate_content

        with (
            patch("core.gemini.idea_generator.genai.Client", return_value=client) as create_client,
            patch(
                "core.gemini.idea_generator.get_setting",
                side_effect=lambda key, default=None: {
                    "GEMINI_API_KEY": "test-key",
                    "GEMINI_MODEL": "gemini-3.8-flash",
                    "IDEA_STYLE": "Đổi vai, hài hiểu lầm ở chợ",
                }[key],
            ),
        ):
            result = generate_ideas()

        create_client.assert_called_once_with(
            api_key="test-key", http_options={"timeout": 60000}
        )
        self.assertEqual(
            result,
            [
                {
                    **idea,
                    "character_1": {"ingredient": idea["character_1"]["ingredient"]},
                    "character_2": {"ingredient": idea["character_2"]["ingredient"]},
                    "duration_seconds": 10,
                    "social_posts": _normalize_social_posts(
                        None,
                        fallback_title=idea["title"],
                        scenes=idea["scenes"],
                    ),
                }
                for idea in ideas
            ],
        )
        call = generate_content.call_args.kwargs
        self.assertEqual(call["model"], "gemini-3.8-flash")
        self.assertIn("Đổi vai, hài hiểu lầm ở chợ", call["contents"])
        self.assertIn("No additional user prompt.", call["contents"])
        self.assertIn("exactly 3 scenes", call["contents"])
        self.assertIn("about 10 seconds", call["contents"])
        self.assertIn("featuring the SAME duo", call["contents"])
        self.assertIn(
            "`@khi` is a mischievous, sharp-tongued monkey who owns a neighborhood motorbike repair shop",
            call["contents"],
        )
        self.assertIn(
            "`@nguoi` is a proud, street-smart middle-aged Vietnamese uncle",
            call["contents"],
        )
        self.assertNotIn("Ăn Phở Bò", call["contents"])
        self.assertIn("mischievous, sharp-tongued monkey", call["contents"])
        self.assertIn("social_posts", call["contents"])
        self.assertEqual(
            call["config"],
            {"response_mime_type": "application/json", "temperature": 0.9},
        )
        client.close.assert_called_once_with()

    def test_validate_ideas_accepts_requested_scene_count(self) -> None:
        idea = {
            "title": "Custom scene count",
            "character_1": {"ingredient": "@khi_tho", "image_prompt": "Rabbit"},
            "character_2": {"ingredient": "@khach", "image_prompt": "Person"},
            "scenes": [
                {"duration": "0-15s", "prompt": "Full story", "type": "story"}
            ],
        }

        self.assertEqual(_validate_ideas([idea, idea], scene_count=1), [idea, idea])

    def test_generate_ideas_uses_custom_brief_scene_count_and_duration(self) -> None:
        idea = {
            "title": "Custom brief",
            "character_1": {"ingredient": "@khi_tho", "image_prompt": "Rabbit"},
            "character_2": {"ingredient": "@khach", "image_prompt": "Person"},
            "scenes": [
                {"duration": "0-15s", "prompt": "The requested story", "type": "story"}
            ],
        }
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps([idea, idea])
        )

        with (
            patch("core.gemini.idea_generator.genai.Client", return_value=client),
            patch(
                "core.gemini.idea_generator.get_setting",
                side_effect=lambda key, default=None: "test-key"
                if key == "GEMINI_API_KEY"
                else default,
            ),
        ):
            result = generate_ideas(
                prompt="Thỏ thi nấu ăn ở chợ",
                scene_count=1,
                duration_seconds=15,
            )

        call = client.models.generate_content.call_args.kwargs
        self.assertIn("YÊU CẦU CHỦ ĐỀ ƯU TIÊN", call["contents"])
        self.assertIn("Bám sát chủ đề người dùng đưa ra", call["contents"])
        self.assertIn("khỉ chủ tiệm sửa xe và @nguoi là ông chú", call["contents"])
        self.assertIn("Thỏ thi nấu ăn ở chợ", call["contents"])
        self.assertIn("exactly 1 scenes", call["contents"])
        self.assertIn("about 15 seconds", call["contents"])
        self.assertEqual(result[0]["duration_seconds"], 15)
        self.assertEqual(result[0]["user_prompt"], "Thỏ thi nấu ăn ở chợ")
        self.assertIn("tiktok", result[0]["social_posts"])
        self.assertIn("facebook", result[0]["social_posts"])
        self.assertIn("youtube", result[0]["social_posts"])

    def test_generate_ideas_rejects_scene_count_outside_supported_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            generate_ideas(scene_count=21)

    def test_generate_ideas_falls_back_to_next_free_model_on_503(self) -> None:
        idea = {
            "title": "Fallback idea",
            "character_1": {"ingredient": "@khi", "image_prompt": "Monkey"},
            "character_2": {"ingredient": "@nguoi", "image_prompt": "Person"},
            "scenes": [
                {"duration": "0-10s", "prompt": "Action", "type": "story"}
            ],
        }
        client = Mock()
        client.models.generate_content.side_effect = [
            RuntimeError("503 UNAVAILABLE: This model is currently experiencing high demand."),
            SimpleNamespace(text=json.dumps([idea, idea])),
        ]

        with (
            patch("core.gemini.idea_generator.genai.Client", return_value=client),
            patch(
                "core.gemini.idea_generator.get_setting",
                side_effect=lambda key, default=None: "test-key"
                if key == "GEMINI_API_KEY"
                else default,
            ),
        ):
            result = generate_ideas(scene_count=1, duration_seconds=10)

        self.assertEqual(len(result), 2)
        self.assertEqual(client.models.generate_content.call_count, 2)
        first_call_model = client.models.generate_content.call_args_list[0].kwargs["model"]
        second_call_model = client.models.generate_content.call_args_list[1].kwargs["model"]
        self.assertNotEqual(first_call_model, second_call_model)
        self.assertEqual(second_call_model, "gemini-flash-latest")

    def test_generate_ideas_falls_back_to_backup_api_key_when_primary_fails(self) -> None:
        idea = {
            "title": "Backup key idea",
            "character_1": {"ingredient": "@khi", "image_prompt": "Monkey"},
            "character_2": {"ingredient": "@nguoi", "image_prompt": "Person"},
            "scenes": [
                {"duration": "0-10s", "prompt": "Action", "type": "story"}
            ],
        }
        client1 = Mock()
        client1.models.generate_content.side_effect = RuntimeError("429 Quota exceeded")
        client2 = Mock()
        client2.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps([idea, idea])
        )

        with (
            patch(
                "core.gemini.idea_generator.genai.Client",
                side_effect=[client1, client2],
            ) as create_client,
            patch(
                "core.gemini.idea_generator.get_setting",
                side_effect=lambda key, default=None: {
                    "GEMINI_API_KEY": "key-1",
                    "GEMINI_FALLBACK_API_KEYS": "key-2",
                }.get(key, default)
                if key in {"GEMINI_API_KEY", "GEMINI_FALLBACK_API_KEYS"} or (default and default.strip())
                else (_ for _ in ()).throw(ValueError(f"Missing required environment variable: {key}")),
            ),
        ):
            result = generate_ideas(scene_count=1, duration_seconds=10)

        self.assertEqual(len(result), 2)
        self.assertEqual(create_client.call_args_list[0].kwargs["api_key"], "key-1")
        self.assertEqual(create_client.call_args_list[1].kwargs["api_key"], "key-2")
        client1.close.assert_called_once_with()
        client2.close.assert_called_once_with()

    def test_candidate_api_keys_includes_numbered_environment_keys(self) -> None:
        settings = {
            "GEMINI_API_KEY": "primary",
            "GEMINI_FALLBACK_API_KEYS": "backup-one",
            "GEMINI_API_KEY1": "primary",
            "GEMINI_API_KEY2": "backup-two",
            "GEMINI_API_KEY3": "",
            "GEMINI_API_KEY4": "backup-four",
            "GEMINI_API_KEY_1": "backup-underscore-one",
        }

        with patch(
            "core.gemini.idea_generator.get_setting",
            side_effect=lambda key, default=None: settings.get(key, default),
        ):
            keys = _candidate_api_keys()

        self.assertEqual(
            keys,
            [
                "primary",
                "backup-one",
                "backup-two",
                "backup-four",
                "backup-underscore-one",
            ],
        )

    def test_generate_ideas_falls_back_when_client_creation_fails(self) -> None:
        idea = {
            "title": "Fallback idea",
            "character_1": {"ingredient": "@khi", "image_prompt": "Monkey"},
            "character_2": {"ingredient": "@nguoi", "image_prompt": "Person"},
            "scenes": [
                {"duration": "0-10s", "prompt": "Action", "type": "story"}
            ],
        }
        backup_client = Mock()
        backup_client.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps([idea, idea])
        )

        with (
            patch(
                "core.gemini.idea_generator.genai.Client",
                side_effect=[RuntimeError("Client initialization failed"), backup_client],
            ) as create_client,
            patch(
                "core.gemini.idea_generator.get_setting",
                side_effect=lambda key, default=None: {
                    "GEMINI_API_KEY": "key-1",
                    "GEMINI_FALLBACK_API_KEYS": "key-2",
                }.get(key, default),
            ),
        ):
            result = generate_ideas(scene_count=1, duration_seconds=10)

        self.assertEqual(len(result), 2)
        self.assertEqual(create_client.call_count, 2)
        self.assertEqual(create_client.call_args_list[1].kwargs["api_key"], "key-2")
        backup_client.close.assert_called_once_with()

    def test_generate_social_posts_for_video_returns_per_platform_captions(self) -> None:
        ai_payload = {
            "general_title": "Thắng Kêu Tra Dầu Ăn",
            "tiktok": {
                "title": "Hook TikTok dầu ăn",
                "caption": "Caption TikTok cực mặn",
                "hashtags": "#xuhuong #tiktokvn #suaxe",
            },
            "facebook": {
                "title": "Tiêu đề Reels FB",
                "caption": "Caption Reels FB hài hước",
                "hashtags": "#reelsvn #facebookreels",
            },
            "youtube": {
                "title": "Thắng Kêu Tra Dầu Ăn #Shorts",
                "caption": "Mô tả YouTube Shorts chuẩn SEO",
                "hashtags": "#Shorts #youtubeshorts",
            },
        }
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps(ai_payload)
        )
        with (
            patch(            "core.gemini.idea_generator.genai.Client", return_value=client),
            patch(
                "core.gemini.idea_generator.get_setting",
                side_effect=lambda key, default=None: "test-key"
                if key == "GEMINI_API_KEY"
                else default,
            ),
        ):
            posts = generate_social_posts_for_video(
                video_title="thang_keu_tra_dau_an",
                hint_text="Khách tự tra dầu ăn vào phanh đĩa",
            )

        self.assertEqual(posts["tiktok"]["caption"], "Caption TikTok cực mặn")
        self.assertEqual(posts["facebook"]["caption"], "Caption Reels FB hài hước")
        self.assertEqual(
            posts["youtube"]["title"],
            "Thắng Kêu Tra Dầu Ăn #Shorts #youtubeshorts",
        )
        self.assertIn("#Shorts", posts["youtube"]["hashtags"])
        self.assertLessEqual(len(posts["youtube"]["title"]), 100)

    def test_default_social_posts_do_not_force_monkey_branding(self) -> None:
        posts = _normalize_social_posts(None, fallback_title="Chuyện xóm trọ")

        for platform_posts in posts.values():
            self.assertNotIn("dunglakhi", platform_posts["hashtags"])
        self.assertIn("#chuyendoithuong", posts["youtube"]["hashtags"])

    def test_generate_ideas_embeds_skill_md_rules_and_character_visuals(self) -> None:
        idea = {
            "title": "Idea 1",
            "character_1": {"ingredient": "@khi"},
            "character_2": {"ingredient": "@nguoi"},
            "scenes": [
                {"duration": "auto", "prompt": "Scene 1", "type": "dialogue"},
            ],
        }
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps([idea, idea])
        )
        with (
            patch("core.gemini.idea_generator.genai.Client", return_value=client),
            patch(
                "core.gemini.idea_generator.get_setting",
                side_effect=lambda key, default=None: "test-key"
                if key == "GEMINI_API_KEY"
                else default,
            ),
        ):
            generate_ideas(scene_count=1, duration_seconds=10, ideas_count=2)

        prompt_sent = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("VIDEO-HÀI-TWO-SHOT-CREATOR", prompt_sent)
        self.assertIn("dirty greasy white tank top and dark grey mechanic work trousers", prompt_sent)
        self.assertIn("deep gruff older Vietnamese lip sync", prompt_sent)
        self.assertIn("high-pitched mischievous Vietnamese lip sync", prompt_sent)
        self.assertIn("looking directly at each other", prompt_sent)
        self.assertIn("remains completely silent with closed mouth", prompt_sent)
        self.assertIn("Đúng là khỉ!", prompt_sent)
        self.assertIn("bursts into cheerful, playful hearty laughter", prompt_sent)


    def test_sanitize_flow_prompt_softens_policy_triggering_words(self) -> None:
        from core.gemini.idea_generator import sanitize_flow_prompt

        cleaned = sanitize_flow_prompt(
            'lip sync: "Chém đẹp thế này, đúng là khỉ!" slapping thigh violently'
        )
        self.assertNotIn("Chém", cleaned)
        self.assertNotIn("violently", cleaned)
        self.assertIn("Cắt đẹp thế này, đúng là khỉ!", cleaned)


if __name__ == "__main__":
    unittest.main()
