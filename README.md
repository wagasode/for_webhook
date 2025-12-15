# for_webhook

## nippo-reminder メッセージ
- 文面は `scripts/messages/morning.txt` と `scripts/messages/night.txt` に分離
- `night.txt` は `{user_id}` と `{today}` を `DISCORD_USER_ID`、当日日付で置換して送信
- 2000文字制限にかからないようスクリプト側で分割投稿するため、テンプレ変更時は文字数だけ注意
