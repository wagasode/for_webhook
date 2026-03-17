# for_webhook

## nippo-reminder メッセージ
- 文面は `scripts/messages/morning.txt` と `scripts/messages/night.txt` に分離
- `night.txt` は `{user_id}` と `{today}` を `DISCORD_USER_ID`、当日日付で置換して送信
- 2000文字制限にかからないようスクリプト側で分割投稿するため、テンプレ変更時は文字数だけ注意

## thread-user-digest（日次集計）
- スクリプト: `scripts/collect_thread_owner_stats.py`
- Workflow: `.github/workflows/thread_owner_digest.yaml`
- 役割: 指定フォーラムの public thread を対象に、指定ユーザーの発言を日次集計して専用スレッドへ投稿
- 詳細ドキュメント: `docs/thread-owner-digest.md`

### 必須 Secrets
- `DISCORD_BOT_TOKEN`
- `DISCORD_FORUM_CHANNEL_IDS`（推奨。複数IDをカンマ/改行区切りで管理）
- `DISCORD_TARGET_USER_ID`
- `DISCORD_WEBHOOK_URL`
- `DISCORD_REPORT_THREAD_ID`

### 互換 Secrets（任意）
- `DISCORD_FORUM_CHANNEL_ID`（単一フォーラム運用向け。`DISCORD_FORUM_CHANNEL_IDS` 未設定時に使用）

### 手動実行入力
- `target_date`（任意、`YYYY-MM-DD`、未指定時は前日JST）
- `dry_run`（`true` でDiscord投稿せずログ出力のみ）
