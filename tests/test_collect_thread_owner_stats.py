import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.collect_thread_owner_stats import (
    RawMessage,
    StructuredEntry,
    build_log_file_paths,
    build_review_report,
    build_structured_entries,
    day_bounds_utc,
    extract_issue_text,
    extract_matchup,
    extract_next_action_text,
    extract_result,
    extract_top_tokens,
    parse_forum_channel_ids,
    split_for_discord,
    validate_llm_payload,
    save_structured_sqlite,
)


class CollectThreadOwnerStatsTest(unittest.TestCase):
    def test_day_bounds_utc_for_jst_date(self) -> None:
        start_utc, end_utc = day_bounds_utc(date(2026, 3, 15))
        self.assertEqual(start_utc.isoformat(), "2026-03-14T15:00:00+00:00")
        self.assertEqual(end_utc.isoformat(), "2026-03-15T15:00:00+00:00")

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

    def test_rule_extract_matchup_result_issue_next(self) -> None:
        deck_keywords = ["AF", "進化ネメシス"]

        self.assertEqual(extract_matchup("vs AF で練習", deck_keywords), "AF")
        self.assertEqual(extract_matchup("進化ネメシス対面のプラン曖昧", deck_keywords), "進化ネメシス")
        self.assertIsNone(extract_matchup("今日は反省だけ書く", deck_keywords))

        self.assertEqual(extract_result("3-2 だった"), "3-2")
        self.assertEqual(extract_result("2勝1敗"), "2-1")
        self.assertEqual(extract_result("W"), "W")
        self.assertIsNone(extract_result("勝敗は未記録"))

        self.assertEqual(extract_issue_text("課題: aimがずれる"), "aimがずれる")
        self.assertEqual(extract_issue_text("課題"), "(詳細なし)")
        self.assertIsNone(extract_issue_text("メモ: 課題あり"))

        self.assertEqual(extract_next_action_text("次: AFマリガン10回確認"), "AFマリガン10回確認")
        self.assertEqual(extract_next_action_text("次回: 連敗時に3分停止"), "連敗時に3分停止")
        self.assertIsNone(extract_next_action_text("あとでやる"))

    def test_build_structured_entries_rule_and_llm_fallback(self) -> None:
        target = date(2026, 3, 15)
        raw_messages = [
            RawMessage(
                message_id="1",
                thread_id="t1",
                thread_name="thread-1",
                timestamp_utc="2026-03-15T01:00:00+00:00",
                raw_text="課題: AF対面のマリガンが曖昧",
            ),
            RawMessage(
                message_id="2",
                thread_id="t1",
                thread_name="thread-1",
                timestamp_utc="2026-03-15T02:00:00+00:00",
                raw_text="7ターン目のプランが固まってない",
            ),
        ]

        calls: list[str] = []

        def fake_llm(text: str) -> tuple[dict[str, object] | None, str | None]:
            calls.append(text)
            return (
                {
                    "matchup": "AF",
                    "result": None,
                    "issue": "7ターン目プラン曖昧",
                    "next_action": "7ターン目分岐を先後で整理",
                    "confidence": 0.84,
                    "reason_short": "文脈補完",
                },
                None,
            )

        entries, stats = build_structured_entries(
            raw_messages=raw_messages,
            target=target,
            deck_keywords=["AF"],
            llm_extractor=fake_llm,
            llm_max_fallback_messages=200,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(stats["raw_count"], 2)
        self.assertEqual(stats["structured_count"], 2)
        self.assertEqual(stats["unclassified_count"], 0)
        self.assertEqual(stats["llm_attempted"], 1)
        self.assertEqual(stats["llm_succeeded"], 1)
        self.assertEqual(entries[0].extract_method, "rule")
        self.assertEqual(entries[1].extract_method, "llm")
        self.assertEqual(entries[1].status, "classified")
        self.assertEqual(entries[1].next_action, "7ターン目分岐を先後で整理")

    def test_build_structured_entries_llm_failure_keeps_unclassified(self) -> None:
        target = date(2026, 3, 15)
        raw_messages = [
            RawMessage(
                message_id="1",
                thread_id="t1",
                thread_name="thread-1",
                timestamp_utc="2026-03-15T01:00:00+00:00",
                raw_text="特に構造がないメモ",
            )
        ]

        def fake_llm(_: str) -> tuple[dict[str, object] | None, str | None]:
            return None, "invalid-json"

        entries, stats = build_structured_entries(
            raw_messages=raw_messages,
            target=target,
            deck_keywords=["AF"],
            llm_extractor=fake_llm,
            llm_max_fallback_messages=200,
        )

        self.assertEqual(entries[0].status, "unclassified")
        self.assertEqual(entries[0].extract_method, "llm_failed")
        self.assertEqual(stats["llm_failed"], 1)
        self.assertEqual(stats["unclassified_count"], 1)

    def test_validate_llm_payload(self) -> None:
        valid = validate_llm_payload(
            {
                "matchup": "AF",
                "result": "W",
                "issue": "焦り",
                "next_action": "3分停止",
                "confidence": "0.9",
                "reason_short": "ok",
            }
        )
        self.assertIsNotNone(valid)
        assert valid is not None
        self.assertEqual(valid["matchup"], "AF")
        self.assertEqual(valid["confidence"], 0.9)

        invalid = validate_llm_payload({"matchup": ["AF"]})
        self.assertIsNone(invalid)

    def test_save_structured_sqlite_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "sample.sqlite3"
            first = StructuredEntry(
                target_date_jst="2026-03-15",
                message_id="m1",
                thread_id="t1",
                thread_name="thread-1",
                timestamp_utc="2026-03-15T01:00:00+00:00",
                raw_text="課題: old",
                matchup=None,
                result=None,
                issue="old",
                next_action=None,
                extract_method="rule",
                confidence=0.7,
                status="classified",
            )
            second = StructuredEntry(
                target_date_jst="2026-03-15",
                message_id="m1",
                thread_id="t1",
                thread_name="thread-1",
                timestamp_utc="2026-03-15T01:00:00+00:00",
                raw_text="課題: new",
                matchup=None,
                result=None,
                issue="new",
                next_action="next",
                extract_method="llm",
                confidence=0.8,
                status="classified",
            )

            save_structured_sqlite(db_path, [first])
            save_structured_sqlite(db_path, [second])

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT COUNT(*), issue, next_action, extract_method FROM structured_logs WHERE message_id = ?",
                    ("m1",),
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(row[0], 1)
            self.assertEqual(row[1], "new")
            self.assertEqual(row[2], "next")
            self.assertEqual(row[3], "llm")

    def test_build_review_report_contains_required_sections(self) -> None:
        entries = [
            StructuredEntry(
                target_date_jst="2026-03-15",
                message_id="m1",
                thread_id="thread-1",
                thread_name="sample",
                timestamp_utc="2026-03-15T01:00:00+00:00",
                raw_text="課題: AFマリガンが曖昧",
                matchup="AF",
                result="2-1",
                issue="AFマリガンが曖昧",
                next_action="先後別マリガン表を作る",
                extract_method="rule",
                confidence=0.7,
                status="classified",
            ),
            StructuredEntry(
                target_date_jst="2026-03-15",
                message_id="m2",
                thread_id="thread-2",
                thread_name="sample2",
                timestamp_utc="2026-03-15T02:00:00+00:00",
                raw_text="自由記述",
                matchup=None,
                result=None,
                issue=None,
                next_action=None,
                extract_method="llm_failed",
                confidence=0.0,
                status="unclassified",
            ),
        ]

        report = build_review_report(
            target=date(2026, 3, 15),
            entries=entries,
            warnings=["OPENAI_API_KEY未設定のためrule-only modeで実行"],
        )

        self.assertIn("- 対象期間: 2026-03-15 00:00:00 - 2026-03-15 23:59:59 JST", report)
        self.assertIn("【推定戦績】", report)
        self.assertIn("2勝1敗 (3戦)", report)
        self.assertIn("【頻出課題 上位5】", report)
        self.assertIn("AFマリガンが曖昧 (1)", report)
        self.assertIn("【次回アクション候補 上位5】", report)
        self.assertIn("先後別マリガン表を作る (1)", report)
        self.assertIn("【未分類メモ件数】", report)
        self.assertIn("- 1件", report)
        self.assertIn("【昨日見つけた課題一覧 (1件)】", report)
        self.assertIn("<#thread-1>", report)

    def test_build_log_file_paths(self) -> None:
        raw_path, sqlite_path, summary_path = build_log_file_paths(date(2026, 3, 15), "artifacts")
        self.assertEqual(raw_path.as_posix(), "artifacts/thread_user_digest_2026-03-15_raw_messages.json")
        self.assertEqual(sqlite_path.as_posix(), "artifacts/thread_user_digest_2026-03-15_structured.sqlite3")
        self.assertEqual(summary_path.as_posix(), "artifacts/thread_user_digest_2026-03-15_summary.json")


if __name__ == "__main__":
    unittest.main()
