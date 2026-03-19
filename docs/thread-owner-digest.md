# Thread User Digest v2

## 機能概要
- 指定フォーラム群の `public thread` から、指定ユーザーの自由記述メッセージを日次で収集する。
- 入力時のタグ付けは不要。後段で抽出・要約して review レーンへ投稿する。
- 実行基盤は `GitHub Actions + Python` の日次バッチ。

## パイプライン
- 処理順は固定: `raw収集 -> ルール抽出 -> 未一致のみLLM抽出 -> 正規化保存 -> 日次レビュー生成`
- ルール抽出対象（5項目）:
  - `matchup`（`vs`, `対面`, 既知デッキ語彙）
  - `result`（`W/L`, `勝/負`, `3-2`）
  - `issue`（`課題` プレフィックス）
  - `next_action`（`次`, `次回`, `next:`）
- LLM抽出はルール未一致メモのみ対象。
- LLM JSONは固定キー: `matchup`, `result`, `issue`, `next_action`, `confidence`, `reason_short`
- LLM失敗時は `status=unclassified` で保存し、レポートに件数表示。

## 保存仕様
- 保存先: `artifacts/`
- 命名規則:
  - `thread_user_digest_YYYY-MM-DD_raw_messages.json`
  - `thread_user_digest_YYYY-MM-DD_structured.sqlite3`
  - `thread_user_digest_YYYY-MM-DD_summary.json`
- SQLiteテーブル: `structured_logs`
- カラム:
  - `target_date_jst`, `message_id`, `thread_id`, `thread_name`, `timestamp_utc`, `raw_text`
  - `matchup`, `result`, `issue`, `next_action`, `extract_method`, `confidence`, `status`
- `message_id` は upsert で重複更新。

## レポート仕様
- 投稿先: `DISCORD_REPORT_THREAD_ID`（review レーン）
- 表示項目:
  - `対象期間`
  - `推定戦績`
  - `頻出課題`
  - `次回アクション候補`
  - `未分類メモ件数`
  - `昨日見つけた課題一覧`
- Discord 2000文字制限に合わせて分割投稿。

## 設定値
### 必須 Secrets / Env
- `DISCORD_BOT_TOKEN`
- `DISCORD_FORUM_CHANNEL_IDS`（推奨、複数IDをカンマ/改行区切り）
- `DISCORD_TARGET_USER_ID`
- `DISCORD_WEBHOOK_URL`
- `DISCORD_REPORT_THREAD_ID`
- `OPENAI_API_KEY`（未設定時は rule-only mode）

### 互換入力
- `DISCORD_FORUM_CHANNEL_ID`（単一フォーラム運用のフォールバック）

### 任意入力
- `OPENAI_MODEL`（default: `gpt-4.1-mini`）
- `LLM_MAX_FALLBACK_MESSAGES`（default: `200`）
- `LLM_TIMEOUT_SECONDS`（default: `20`）
- `TARGET_DATE`（`YYYY-MM-DD`、未指定時は前日JST）
- `DRY_RUN`（`true` で投稿せずログ出力）
- `OUTPUT_DIR`（default: `artifacts`）

## Workflow
- ファイル: `.github/workflows/thread_owner_digest.yaml`
- トリガー:
  - `schedule`: 毎日 08:10 JST（`10 23 * * *` UTC）
  - `workflow_dispatch`: 手動実行（`target_date`, `dry_run`）
- 依存:
  - `pip install openai`
- artifact upload:
  - `artifacts/*.json`
  - `artifacts/*.sqlite3`

## テスト観点
- ルール抽出（対面・勝敗・課題・次回アクション）
- 未一致メモのみLLMフォールバック
- LLM失敗時の `unclassified` 保存
- SQLite upsert と件数整合
- レポート必須項目（対象期間・未分類件数・課題一覧）
- 文字数分割（Discord投稿上限）
