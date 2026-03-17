import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


API_BASE = "https://discord.com/api/v10"
JST = ZoneInfo("Asia/Tokyo")
PUBLIC_THREAD_TYPE = 11
MAX_DISCORD_CHARS = 1900

URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"<[@#][!&]?\d+>")
CUSTOM_EMOJI_RE = re.compile(r"<a?:[a-zA-Z0-9_]+:\d+>")
TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[ぁ-んァ-ン一-龥ー]{2,}")
KATAKANA_RE = re.compile(r"[ァ-ヴー]{2,}")
KANJI_RE = re.compile(r"[一-龥]{2,}")
STOPWORDS = {
    "です",
    "ます",
    "する",
    "して",
    "した",
    "してる",
    "ある",
    "いる",
    "こと",
    "これ",
    "それ",
    "ため",
    "with",
    "from",
    "this",
    "that",
    "your",
    "http",
    "https",
}


@dataclass
class TargetUserMessage:
    thread_id: str
    thread_name: str
    timestamp: str
    content: str


def parse_iso8601(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def resolve_target_date(raw: str | None) -> date:
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    return (datetime.now(JST) - timedelta(days=1)).date()


def parse_forum_channel_ids(raw_ids: str | None, single_id: str | None = None) -> list[str]:
    values: list[str] = []
    if raw_ids:
        values.extend(re.split(r"[\s,]+", raw_ids.strip()))
    if single_id:
        values.append(single_id.strip())

    uniq: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        uniq.append(value)
    return uniq


def day_bounds_utc(target: date) -> tuple[datetime, datetime]:
    start_jst = datetime.combine(target, time.min, tzinfo=JST)
    end_jst = start_jst + timedelta(days=1)
    return start_jst.astimezone(timezone.utc), end_jst.astimezone(timezone.utc)


def is_timestamp_in_jst_date(timestamp: str, target: date) -> bool:
    dt_jst = parse_iso8601(timestamp).astimezone(JST)
    return dt_jst.date() == target


def normalize_text(text: str) -> str:
    cleaned = URL_RE.sub(" ", text)
    cleaned = MENTION_RE.sub(" ", cleaned)
    cleaned = CUSTOM_EMOJI_RE.sub(" ", cleaned)
    return cleaned


def extract_top_tokens(texts: list[str], limit: int = 10) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()

    def add_token(raw: str) -> None:
        normalized = raw.lower()
        if normalized.isdigit():
            return
        if normalized in STOPWORDS:
            return
        if len(normalized) < 2:
            return
        counter[normalized] += 1

    for text in texts:
        for token in TOKEN_RE.findall(normalize_text(text)):
            # Japanese contiguous text often includes particles, so also lift
            # likely keyword units (kanji compounds / katakana words).
            subs = KATAKANA_RE.findall(token) + KANJI_RE.findall(token)
            if subs:
                for sub in subs:
                    add_token(sub)
                continue
            add_token(token)
    return counter.most_common(limit)


def summarize_message_content(text: str, limit: int = 80) -> str:
    compact = re.sub(r"\s+", " ", normalize_text(text)).strip()
    if not compact:
        return "(本文なし)"
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def split_for_discord(text: str, max_len: int = MAX_DISCORD_CHARS) -> list[str]:
    if len(text) <= 2000:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_len:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            start = 0
            while start < len(line):
                chunks.append(line[start : start + max_len].rstrip("\n"))
                start += max_len
            continue
        if len(current) + len(line) > max_len:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line
    if current:
        chunks.append(current.rstrip("\n"))
    return [chunk for chunk in chunks if chunk]


def filter_target_user_messages(
    messages: list[dict[str, Any]],
    target_user_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for message in messages:
        author_id = str(message.get("author", {}).get("id", ""))
        if author_id != str(target_user_id):
            continue
        timestamp = message.get("timestamp")
        if not timestamp:
            continue
        created_at = parse_iso8601(timestamp)
        if start_utc <= created_at < end_utc:
            matched.append(message)
    return matched


def build_report(
    target: date,
    forum_channel_ids: list[str],
    target_user_id: str,
    scanned_threads: int,
    target_user_messages: list[TargetUserMessage],
) -> str:
    by_thread: Counter[str] = Counter()
    token_texts: list[str] = []

    for item in target_user_messages:
        by_thread[item.thread_name] += 1
        if item.content:
            token_texts.append(item.content)

    forum_mentions = ", ".join(f"<#{forum_id}>" for forum_id in forum_channel_ids[:10])
    if not forum_mentions:
        forum_mentions = "(なし)"
    if len(forum_channel_ids) > 10:
        forum_mentions += f" ほか{len(forum_channel_ids) - 10}件"
    period_start = datetime.combine(target, time.min, tzinfo=JST)
    period_end = period_start + timedelta(days=1) - timedelta(seconds=1)

    lines = [
        f"ユーザー発言 日次レポート ({target.isoformat()} JST)",
        f"- 対象フォーラム数: {len(forum_channel_ids)}",
        f"- 対象フォーラム: {forum_mentions}",
        f"- 対象ユーザー: <@{target_user_id}>",
        f"- 集計期間: {period_start.strftime('%Y-%m-%d %H:%M:%S')} - {period_end.strftime('%Y-%m-%d %H:%M:%S')} JST",
        f"- 対象スレッド数: {scanned_threads}",
        f"- 総発言数: {len(target_user_messages)}",
        "",
        "【スレッド別件数 上位10】",
    ]

    if by_thread:
        for idx, (thread_name, count) in enumerate(by_thread.most_common(10), start=1):
            lines.append(f"{idx}. {thread_name}: {count}")
    else:
        lines.append("0件")

    lines.extend(["", "【頻出語 上位10】"])
    top_tokens = extract_top_tokens(token_texts, limit=10)
    if top_tokens:
        for token, count in top_tokens:
            lines.append(f"- {token}: {count}")
    else:
        lines.append("- なし")

    lines.extend(["", "【代表発言（最新5件）】"])
    if target_user_messages:
        recent = sorted(target_user_messages, key=lambda x: x.timestamp, reverse=True)[:5]
        for item in recent:
            excerpt = summarize_message_content(item.content, limit=80)
            lines.append(f"- [{item.thread_name}] {excerpt}")
    else:
        lines.append("- なし")

    return "\n".join(lines)


class DiscordClient:
    def __init__(self, bot_token: str):
        self.bot_token = bot_token

    def _request(self, method: str, path: str, params: dict[str, str] | None = None) -> Any:
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(
            url=url,
            method=method,
            headers={
                "Authorization": f"Bot {self.bot_token}",
                "User-Agent": "thread-owner-digest/1.0 (+https://github.com/<owner>/<repo>)",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Discord API error {exc.code} at {path}: {detail}") from exc

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return self._request("GET", f"/channels/{channel_id}")

    def list_forum_threads(self, forum_channel_id: str, start_utc: datetime) -> list[dict[str, Any]]:
        forum = self.get_channel(forum_channel_id)
        guild_id = forum.get("guild_id")
        if not guild_id:
            raise RuntimeError(
                "guild_id を取得できませんでした。フォーラムID（DISCORD_FORUM_CHANNEL_IDS / DISCORD_FORUM_CHANNEL_ID）を確認してください。"
            )

        threads_by_id: dict[str, dict[str, Any]] = {}

        active = self._request("GET", f"/guilds/{guild_id}/threads/active")
        for thread in active.get("threads", []):
            if thread.get("parent_id") != forum_channel_id:
                continue
            if int(thread.get("type", -1)) != PUBLIC_THREAD_TYPE:
                continue
            threads_by_id[str(thread["id"])] = thread

        before: str | None = None
        while True:
            params = {"limit": "100"}
            if before:
                params["before"] = before
            archived = self._request("GET", f"/channels/{forum_channel_id}/threads/archived/public", params=params)
            archived_threads = archived.get("threads", [])
            if not archived_threads:
                break
            for thread in archived_threads:
                if int(thread.get("type", -1)) != PUBLIC_THREAD_TYPE:
                    continue
                threads_by_id[str(thread["id"])] = thread

            oldest_archived = archived_threads[-1]
            archive_ts = oldest_archived.get("thread_metadata", {}).get("archive_timestamp")
            if not archive_ts:
                archive_ts = oldest_archived.get("archive_timestamp")
            if archive_ts and parse_iso8601(archive_ts) < start_utc:
                break

            if not archived.get("has_more"):
                break
            before = archived_threads[-1].get("archive_timestamp")
            if not before:
                break

        return list(threads_by_id.values())

    def list_thread_messages_for_window(
        self,
        thread_id: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        before: str | None = None

        while True:
            params = {"limit": "100"}
            if before:
                params["before"] = before
            messages = self._request("GET", f"/channels/{thread_id}/messages", params=params)
            if not isinstance(messages, list) or not messages:
                break

            for msg in messages:
                ts = msg.get("timestamp")
                if not ts:
                    continue
                created_at = parse_iso8601(ts)
                if start_utc <= created_at < end_utc:
                    result.append(msg)

            oldest = messages[-1].get("timestamp")
            if oldest and parse_iso8601(oldest) < start_utc:
                break

            if len(messages) < 100:
                break
            before = messages[-1].get("id")
            if not before:
                break

        return result


def post_to_webhook(webhook_url: str, thread_id: str, text: str) -> None:
    post_url = f"{webhook_url}?thread_id={thread_id}"
    payload = json.dumps({"content": text}).encode("utf-8")
    req = urllib.request.Request(
        post_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "thread-owner-digest/1.0 (+https://github.com/<owner>/<repo>)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()


def run() -> None:
    bot_token = os.environ["DISCORD_BOT_TOKEN"]
    forum_channel_ids = parse_forum_channel_ids(
        os.environ.get("DISCORD_FORUM_CHANNEL_IDS"),
        os.environ.get("DISCORD_FORUM_CHANNEL_ID"),
    )
    if not forum_channel_ids:
        raise RuntimeError(
            "DISCORD_FORUM_CHANNEL_IDS または DISCORD_FORUM_CHANNEL_ID のいずれかを設定してください。"
        )
    target_user_id = os.environ["DISCORD_TARGET_USER_ID"]
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    report_thread_id = os.environ["DISCORD_REPORT_THREAD_ID"]
    target_date_raw = os.environ.get("TARGET_DATE")
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

    target = resolve_target_date(target_date_raw)
    start_utc, end_utc = day_bounds_utc(target)
    client = DiscordClient(bot_token)

    target_user_messages: list[TargetUserMessage] = []
    scanned_threads = 0

    for forum_channel_id in forum_channel_ids:
        threads = client.list_forum_threads(forum_channel_id, start_utc)
        scanned_threads += len(threads)

        for thread in threads:
            thread_id = str(thread.get("id", ""))
            thread_name = thread.get("name") or f"thread-{thread_id}"
            if not thread_id:
                continue

            messages = client.list_thread_messages_for_window(thread_id, start_utc, end_utc)
            target_only = filter_target_user_messages(messages, target_user_id, start_utc, end_utc)
            for msg in target_only:
                target_user_messages.append(
                    TargetUserMessage(
                        thread_id=thread_id,
                        thread_name=thread_name,
                        timestamp=msg["timestamp"],
                        content=msg.get("content", ""),
                    )
                )

    report = build_report(target, forum_channel_ids, target_user_id, scanned_threads, target_user_messages)
    print(report)

    if dry_run:
        print("DRY_RUN=true のため投稿をスキップしました。")
        return

    for chunk in split_for_discord(report):
        post_to_webhook(webhook_url, report_thread_id, chunk)
    print("Discord へのレポート投稿が完了しました。")


if __name__ == "__main__":
    run()
