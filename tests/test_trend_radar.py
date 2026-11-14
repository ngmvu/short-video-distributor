from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from requests import ConnectionError

from core.trend_radar import (
    GOOGLE_TRENDS_RSS_URL,
    TrendRadarError,
    get_trend_signals,
)


RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
  <channel>
    <item>
      <title>thời tiết</title>
      <ht:approx_traffic>10000+</ht:approx_traffic>
      <pubDate>Sat, 14 Nov 2026 00:00:00 +0000</pubDate>
      <link>https://trends.google.com/trending/rss?geo=VN</link>
    </item>
    <item>
      <title>quán ăn AI</title>
      <ht:approx_traffic>200+</ht:approx_traffic>
      <pubDate>Sat, 14 Nov 2026 00:00:00 +0000</pubDate>
      <link>https://trends.google.com/trending/rss?geo=VN</link>
    </item>
    <item>
      <title>quân nhân</title>
      <ht:approx_traffic>5000+</ht:approx_traffic>
      <pubDate>Sat, 14 Nov 2026 00:00:00 +0000</pubDate>
      <link>https://trends.google.com/trending/rss?geo=VN</link>
      <ht:news_item>
        <ht:news_item_title>6 chiến sĩ hy sinh</ht:news_item_title>
        <ht:news_item_url>https://news.example.vn/story</ht:news_item_url>
      </ht:news_item>
    </item>
    <item>
      <title>tai nạn giao thông</title>
      <ht:approx_traffic>50000+</ht:approx_traffic>
      <pubDate>Sat, 14 Nov 2026 00:00:00 +0000</pubDate>
      <link>https://trends.google.com/trending/rss?geo=VN</link>
    </item>
  </channel>
</rss>
""".encode("utf-8")


class TrendRadarTests(unittest.TestCase):
    def test_fetch_ranks_channel_fit_and_filters_sensitive_news(self) -> None:
        response = Mock()
        response.content = RSS_SAMPLE
        response.raise_for_status.return_value = None

        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "trend_radar.json"
            with (
                patch("core.trend_radar.TREND_CACHE_FILE", cache_path),
                patch("core.trend_radar.requests.get", return_value=response) as get,
            ):
                signals = get_trend_signals()

        self.assertEqual([signal.title for signal in signals], ["quán ăn AI", "thời tiết"])
        self.assertGreater(signals[0].channel_fit_score, signals[1].channel_fit_score)
        self.assertEqual(signals[0].source_url, GOOGLE_TRENDS_RSS_URL)
        self.assertEqual(signals[0].approximate_traffic, "200+")
        self.assertNotIn("quân nhân", [signal.title for signal in signals])
        self.assertNotIn("tai nạn giao thông", [signal.title for signal in signals])
        get.assert_called_once()

    def test_fresh_trend_cache_avoids_refetching(self) -> None:
        response = Mock()
        response.content = RSS_SAMPLE
        response.raise_for_status.return_value = None

        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "trend_radar.json"
            with (
                patch("core.trend_radar.TREND_CACHE_FILE", cache_path),
                patch("core.trend_radar.requests.get", return_value=response) as get,
            ):
                first_result = get_trend_signals()
                second_result = get_trend_signals()

        self.assertEqual(first_result, second_result)
        get.assert_called_once()

    def test_fetch_error_is_explicit(self) -> None:
        with patch(
            "core.trend_radar.requests.get",
            side_effect=ConnectionError("network unavailable"),
        ):
            with self.assertRaisesRegex(
                TrendRadarError, "Could not fetch Google Trends RSS"
            ):
                get_trend_signals()

    def test_invalid_limit_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 50"):
            get_trend_signals(0)
