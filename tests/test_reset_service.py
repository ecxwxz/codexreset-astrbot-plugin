import unittest
from datetime import datetime, timezone

from astrbot_plugin_codex_reset.reset_service import (
    StatusSnapshot,
    format_datetime_cn,
    format_push,
    format_status,
)


class ResetServiceTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "data": {
                "latest_reset": {
                    "id": "123",
                    "reset_type": "regular",
                    "announced_at": "2026-08-21T23:40:12Z",
                    "text": "reset",
                    "source": {"url": "https://x.com/example"},
                },
                "active_watch": {
                    "level": "strong",
                    "reset_chance_percent": 91,
                    "forecast_window": "by 2pm tomorrow",
                    "observed_at": "2026-08-23T06:32:27Z",
                    "expires_at": "2026-08-23T21:00:00Z",
                    "text": "watch",
                    "source": {"url": "https://x.com/watch"},
                },
                "stats": {"total": 45},
            },
            "meta": {"generated_at": "2026-08-23T16:19:26Z"},
        }

    def test_beijing_format(self):
        text = format_datetime_cn("2026-08-23T21:00:00Z")
        self.assertEqual(text, "8月24号 星期一 早上5点")

    def test_status_mentions_prediction_and_disclaimer(self):
        snapshot = StatusSnapshot.from_payload(self.payload)
        text = format_status(
            snapshot,
            now=datetime(2026, 8, 23, 16, tzinfo=timezone.utc),
        )
        self.assertIn("北京时间 8月24号 星期一 早上5点 前后", text)
        self.assertIn("91%", text)
        self.assertIn("不是 OpenAI 官方承诺", text)

    def test_fingerprint_ignores_observation_time(self):
        first = StatusSnapshot.from_payload(self.payload)
        changed = dict(self.payload)
        changed["data"] = dict(self.payload["data"])
        changed["data"]["active_watch"] = dict(self.payload["data"]["active_watch"])
        changed["data"]["active_watch"]["observed_at"] = "2026-08-23T07:32:27Z"
        changed["meta"] = {"generated_at": "2026-08-23T17:19:26Z"}
        second = StatusSnapshot.from_payload(changed)
        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_invalid_timezone_keeps_beijing_fallback(self):
        self.assertEqual(
            format_datetime_cn("2026-08-23T21:00:00Z", "Asia/Shanghai-missing"),
            "8月24号 星期一 早上5点",
        )

    def test_expired_watch_is_not_presented_as_upcoming(self):
        snapshot = StatusSnapshot.from_payload(self.payload)
        text = format_status(
            snapshot,
            now=datetime(2026, 8, 24, 0, tzinfo=timezone.utc),
        )
        self.assertIn("没有可用的下一次重置预测", text)

    def test_push_without_current_data_is_not_blank(self):
        snapshot = StatusSnapshot.from_payload({"data": {}, "meta": {}})
        text = format_push(snapshot, reason="both")
        self.assertIn("没有可用的重置预测或公告变化", text)

    def test_watch_text_revision_changes_fingerprint(self):
        first = StatusSnapshot.from_payload(self.payload)
        changed = dict(self.payload)
        changed["data"] = dict(self.payload["data"])
        changed["data"]["active_watch"] = dict(self.payload["data"]["active_watch"])
        changed["data"]["active_watch"]["text"] = "revised watch"
        second = StatusSnapshot.from_payload(changed)
        self.assertNotEqual(first.fingerprint(), second.fingerprint())


if __name__ == "__main__":
    unittest.main()
