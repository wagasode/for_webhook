import unittest
from datetime import date, datetime, timezone

from scripts.collect_thread_owner_stats import (
    extract_top_tokens,
    filter_target_user_messages,
    is_timestamp_in_jst_date,
    parse_forum_channel_ids,
    split_for_discord,
)


class CollectThreadOwnerStatsTest(unittest.TestCase):
    def test_is_timestamp_in_jst_date_boundaries(self) -> None:
        target = date(2026, 3, 15)
        self.assertTrue(is_timestamp_in_jst_date("2026-03-14T15:00:00+00:00", target))
        self.assertTrue(is_timestamp_in_jst_date("2026-03-15T14:59:59+00:00", target))
        self.assertFalse(is_timestamp_in_jst_date("2026-03-15T15:00:00+00:00", target))
        self.assertFalse(is_timestamp_in_jst_date("2026-03-14T14:59:59+00:00", target))

    def test_filter_target_user_messages(self) -> None:
        start_utc = datetime(2026, 3, 14, 15, 0, 0, tzinfo=timezone.utc)
        end_utc = datetime(2026, 3, 15, 15, 0, 0, tzinfo=timezone.utc)
        messages = [
            {
                "author": {"id": "owner"},
                "timestamp": "2026-03-14T15:01:00+00:00",
                "content": "owner in",
            },
            {
                "author": {"id": "other"},
                "timestamp": "2026-03-14T15:02:00+00:00",
                "content": "other in",
            },
            {
                "author": {"id": "owner"},
                "timestamp": "2026-03-15T15:00:00+00:00",
                "content": "owner out",
            },
        ]
        filtered = filter_target_user_messages(messages, "owner", start_utc, end_utc)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["content"], "owner in")

    def test_extract_top_tokens(self) -> None:
        texts = [
            "今日はAPI設計を進める https://example.com",
            "API設計 のレビューをする <@123456>",
            "レビューで改善ポイントを確認する",
        ]
        top = dict(extract_top_tokens(texts, limit=5))
        self.assertIn("api", top)
        self.assertIn("設計", top)
        self.assertIn("レビュー", top)
        self.assertNotIn("https", top)

    def test_split_for_discord(self) -> None:
        line = "a" * 700
        text = "\n".join([line, line, line, line])
        chunks = split_for_discord(text, max_len=1200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1200 for chunk in chunks))

    def test_parse_forum_channel_ids(self) -> None:
        parsed = parse_forum_channel_ids("111, 222\n333 222", "111")
        self.assertEqual(parsed, ["111", "222", "333"])

    def test_parse_forum_channel_ids_fallback(self) -> None:
        parsed = parse_forum_channel_ids(None, "999")
        self.assertEqual(parsed, ["999"])


if __name__ == "__main__":
    unittest.main()
