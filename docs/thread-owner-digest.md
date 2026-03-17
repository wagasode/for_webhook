# Thread Owner Digest v1

## 機能概要
- 指定フォーラム内の `public thread` を対象に、スレ主本人の発言を日次で集計する機能。
- 集計結果は Discord の専用スレッドへ自動投稿する。
- 実装は既存構成（GitHub Actions + Python）を継続利用。

## 追加された構成
- 集計スクリプト: `scripts/collect_thread_owner_stats.py`
- 定期実行ワークフロー: `.github/workflows/thread_owner_digest.yaml`
- 単体テスト: `tests/test_collect_thread_owner_stats.py`

## 集計仕様（v1）
- 対象日: デフォルトは「前日JST」。`TARGET_DATE=YYYY-MM-DD` で指定可能。
- 対象範囲:
  - 指定フォーラム配下の `public thread`
  - 各スレッドで `author.id == owner_id` の投稿のみ
- 出力項目:
  - 総発言数
  - スレッド別件数（上位10）
  - 頻出語（上位10、URL/メンション等を除外した簡易抽出）
  - 代表発言（最新5件、80文字要約）
- 投稿方式: 既存Webhookを使用し、2000文字制限対策として分割投稿。

## 必須Secrets / Env
- `DISCORD_BOT_TOKEN`（読み取り用）
- `DISCORD_FORUM_CHANNEL_ID`（集計対象フォーラム）
- `DISCORD_WEBHOOK_URL`（投稿先Webhook）
- `DISCORD_REPORT_THREAD_ID`（投稿先スレッド）

## 実行方法
- 定期実行: `thread_owner_digest.yaml` の `schedule`（毎日 08:10 JST）。
- 手動実行: `workflow_dispatch`
  - `target_date`（任意、`YYYY-MM-DD`）
  - `dry_run`（`true` の場合は投稿せずログ出力のみ）

## テスト
- 実装済み単体テスト:
  - JST日付境界判定
  - ownerフィルタ
  - 頻出語抽出
  - 文字数分割
- ローカル実行コマンド:
  - `python3 -m unittest discover -s tests -v`

## 既知の制約
- private thread は対象外（v1仕様）。
- 要約はLLM未使用の簡易方式（頻出語 + 代表発言）。
- API権限不足時はWorkflow失敗となり、ログで原因を追跡する。
