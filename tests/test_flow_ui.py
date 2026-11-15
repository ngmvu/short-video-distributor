import re
import unittest
from unittest.mock import patch

from core import flow_ui
from core.gemini import idea_generator


class FlowUiTests(unittest.TestCase):
    def _settings(self, duration: str) -> flow_ui.FlowSettings:
        return flow_ui.FlowSettings(
            mode="scene", input_mode="ingredients", aspect_ratio="9:16",
            video_model="Omni 1.1 Flash", resolution="720p", duration=duration,
            output_count="1", download_quality="1080", attach_voice=True,
        )

    def test_fixed_duration(self) -> None:
        self.assertEqual(flow_ui.pick_duration_seconds(self._settings("10"), 3), 10)

    def test_auto_duration_picks_shortest_fit(self) -> None:
        self.assertEqual(flow_ui.pick_duration_seconds(self._settings("auto"), 5), 6)
        self.assertEqual(flow_ui.pick_duration_seconds(self._settings("auto"), 30), 10)

    def test_prompt_duration_wins_over_setting(self) -> None:
        scene = {"duration": "auto", "prompt": "Medium two-shot, no cuts, duration 8 seconds. SET: shop"}
        requested = flow_ui.requested_duration_seconds(scene)
        self.assertEqual(requested, 8)
        self.assertEqual(flow_ui.pick_duration_seconds(self._settings("10"), 3, requested), 8)

    def test_prompt_duration_snaps_up_to_flow_options(self) -> None:
        for text, expected in (("a 7-second clip", 8), ("duration 15 seconds", 10), ("thời lượng 4 giây", 4)):
            requested = flow_ui.requested_duration_seconds({"prompt": text})
            self.assertEqual(flow_ui.pick_duration_seconds(self._settings("auto"), 6, requested), expected)

    def test_finish_timing_is_not_a_duration(self) -> None:
        scene = {"prompt": "starts speaking immediately at 0s and finishes before 10s, last 2 seconds silent"}
        self.assertIsNone(flow_ui.requested_duration_seconds(scene))

    def test_quality_menu_regex_matches_vietnamese_labels(self) -> None:
        self.assertTrue(flow_ui.quality_menu_regex("1080").search("1080p Đã tăng độ phân giải"))
        self.assertTrue(flow_ui.quality_menu_regex("720").search("720p Kích thước gốc"))
        self.assertFalse(flow_ui.quality_menu_regex("1080").search("720p Kích thước gốc"))

    def test_duration_regex_matches_both_languages(self) -> None:
        pattern = re.compile(flow_ui._duration_regex(10), re.IGNORECASE)
        self.assertTrue(pattern.search("10 giây"))
        self.assertTrue(pattern.search("10s"))
        self.assertFalse(pattern.search("4 giây"))


class FlowCreditsTests(unittest.TestCase):
    def test_parse_credits_vietnamese_and_english(self) -> None:
        menu = "Nguyễn Minh Vũ\nChuyển đổi tài khoản\nmovie_filter_auto\n491 tín dụng Google Flow\nĐặt lại lúc 01:27"
        self.assertEqual(flow_ui.parse_credits(menu), 491)
        self.assertEqual(flow_ui.parse_credits("1,250 Google Flow credits"), 1250)
        self.assertEqual(flow_ui.parse_credits("12 credits remaining"), 12)
        self.assertIsNone(flow_ui.parse_credits("Đặt lại lúc 01:27 vào 7 thg 12"))

    def test_parse_reset_time_from_account_menu(self) -> None:
        from datetime import datetime

        now = datetime(2026, 11, 15, 10, 0).timestamp()
        menu = "Nguyễn Minh Vũ\n1 tín dụng Google Flow\nĐặt lại lúc 01:27 vào 7 thg 12\nNâng cấp"
        info = flow_ui.parse_account_menu(menu, now)
        self.assertEqual(info.credits, 1)
        self.assertEqual(info.reset_at, int(datetime(2026, 12, 7, 1, 27).timestamp()))
        self.assertEqual(info.reset_text, "Đặt lại lúc 01:27 vào 7 thg 12")
        # a date already passed this year means next year
        reset_at, _ = flow_ui.parse_reset("Đặt lại lúc 08:00 vào 3 thg 1", now)
        self.assertEqual(reset_at, int(datetime(2027, 1, 3, 8, 0).timestamp()))
        reset_at, _ = flow_ui.parse_reset("Resets at 1:27 PM on Dec 7", now)
        self.assertEqual(reset_at, int(datetime(2026, 12, 7, 13, 27).timestamp()))
        self.assertEqual(flow_ui.parse_reset("491 tín dụng Google Flow", now), (None, ""))

    def test_format_reset(self) -> None:
        from datetime import datetime

        now = datetime(2026, 11, 15, 10, 0).timestamp()
        reset = int(datetime(2026, 12, 7, 1, 27).timestamp())
        self.assertEqual(flow_ui.format_reset({"reset_at": reset}, now), "hồi credit 01:27 07/12 (còn 21 ngày)")
        self.assertIn("còn 3 giờ", flow_ui.format_reset({"reset_at": now + 3 * 3600 + 60}, now))
        self.assertIn("kiểm tra lại", flow_ui.format_reset({"reset_at": now - 5}, now))
        self.assertEqual(flow_ui.format_reset({"credits": 5}, now), "")

    def test_save_keeps_known_reset_when_not_read(self) -> None:
        import tempfile
        import time
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp, patch("core.config.STORAGE_DIR", Path(tmp)):
            flow_ui.save_profile_credits("a", 10, int(time.time()) + 3600, "Đặt lại …")
            flow_ui.save_profile_credits("a", 9)
            info = flow_ui.load_profile_credits()["a"]
        self.assertEqual(info["credits"], 9)
        self.assertEqual(info["reset_text"], "Đặt lại …")

    def _run_check(self, credits, threshold="20"):
        import asyncio

        from core import flow_runner

        async def fake_read(_page):
            return flow_ui.AccountMenuInfo(credits)

        with patch.object(flow_ui, "read_account_menu", fake_read), \
                patch.object(flow_ui, "get_runtime_pref", return_value=threshold), \
                patch.object(flow_ui, "save_profile_credits"):
            return asyncio.run(flow_runner._ensure_enough_credits(object(), "d"))

    def test_low_credits_switch_profile(self) -> None:
        from core.flow_runner import FlowCreditExhaustedError

        with self.assertRaises(FlowCreditExhaustedError):
            self._run_check(19)

    def test_enough_credits_continue(self) -> None:
        self.assertIsNone(self._run_check(20))
        self.assertIsNone(self._run_check(491))

    def test_unknown_or_disabled_continue(self) -> None:
        self.assertIsNone(self._run_check(None))
        self.assertIsNone(self._run_check(3, threshold="0"))


class ProfileFailoverTests(unittest.TestCase):
    """Any error on one account (unusual activity, timeouts...) -> next account renders the rest."""

    def _run(self, failures: dict):
        import asyncio
        import tempfile
        from pathlib import Path

        from core import flow_runner as fr

        tmp = Path(tempfile.mkdtemp())
        profiles = [
            fr.FlowProfile(name=name, auth_file=tmp / f"{name}.json", profile_dir=tmp / f"missing-{name}")
            for name in ("d", "a")
        ]
        for prof in profiles:
            prof.auth_file.write_text('{"cookies": []}', encoding="utf-8")
        calls: list[tuple[str, int]] = []

        async def fake_clip(*args, **kwargs):
            filename = args[8]
            scene = int(filename.rsplit("scene", 1)[1].split(".")[0])
            calls.append((kwargs["profile_name"], scene))
            error = failures.get((kwargs["profile_name"], scene))
            if error:
                raise error
            path = tmp / filename
            path.write_bytes(f"{kwargs['profile_name']}-{scene}".encode())
            return path

        class FakeContext:
            pages = [object()]

            async def close(self):
                pass

        class FakeBrowser:
            async def new_context(self, **_):
                return FakeContext()

            async def close(self):
                pass

        class FakeChromium:
            async def launch(self, **_):
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        idea = {
            "title": "Test",
            "scenes": [
                {"prompt": f"Scene {i}", "first_frame": "@nguoi + @khi + @Voice_Nguoi", "duration": "auto"}
                for i in (1, 2, 3)
            ],
        }
        with patch.object(fr, "list_flow_profiles", return_value=profiles), \
                patch.object(fr, "async_playwright", lambda: FakePlaywright()), \
                patch.object(fr, "_generate_one_clip", fake_clip), \
                patch.object(fr, "ensure_storage"), \
                patch.object(flow_ui, "save_debug_screenshot"), \
                patch.object(flow_ui, "get_runtime_pref", side_effect=lambda k, d="": {"FLOW_GENERATION_MODE": "scene"}.get(k, d)):
            clips = asyncio.run(fr.generate_video_clips("item1", idea))
        return calls, clips

    def test_unusual_activity_switches_account_and_keeps_done_scenes(self) -> None:
        from core.flow_runner import FlowGenerationFailedError

        calls, clips = self._run({("d", 2): FlowGenerationFailedError("unusual activity")})
        self.assertEqual(calls, [("d", 1), ("d", 2), ("a", 2), ("a", 3)])
        self.assertEqual([c.read_bytes().decode() for c in clips], ["d-1", "a-2", "a-3"])

    def test_any_playwright_error_switches_account(self) -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        calls, clips = self._run({("d", 1): PlaywrightTimeoutError("Locator.click: Timeout 8000ms exceeded.")})
        self.assertEqual(calls, [("d", 1), ("a", 1), ("a", 2), ("a", 3)])
        self.assertEqual(len(clips), 3)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class GeminiKeyRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        idea_generator._EXHAUSTED_API_KEYS.clear()
        idea_generator._DEAD_API_KEYS.clear()

    def tearDown(self) -> None:
        idea_generator._EXHAUSTED_API_KEYS.clear()
        idea_generator._DEAD_API_KEYS.clear()

    def test_quota_exhausted_key_is_skipped_on_next_call(self) -> None:
        calls: list[str] = []

        class FakeModels:
            def __init__(self, key: str) -> None:
                self.key = key

            def generate_content(self, **_: object) -> _FakeResponse:
                calls.append(self.key)
                if self.key == "key-1":
                    raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
                return _FakeResponse('{"ok": true}')

        class FakeClient:
            def __init__(self, api_key: str, **_: object) -> None:
                self.models = FakeModels(api_key)

            def close(self) -> None:
                pass

        with patch.object(idea_generator, "_candidate_api_keys", return_value=["key-1", "key-2"]), \
                patch.object(idea_generator, "_candidate_models", return_value=["m"]), \
                patch.object(idea_generator.genai, "Client", FakeClient):
            self.assertEqual(idea_generator._call_gemini_json("x"), '{"ok": true}')
            self.assertEqual(calls, ["key-1", "key-2"])
            calls.clear()
            idea_generator._call_gemini_json("y")
            self.assertEqual(calls, ["key-2"])  # key-1 is cooling down after the 429


if __name__ == "__main__":
    unittest.main()
