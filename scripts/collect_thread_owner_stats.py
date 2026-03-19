import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


API_BASE = "https://discord.com/api/v10"
JST = ZoneInfo("Asia/Tokyo")
PUBLIC_THREAD_TYPE = 11
MAX_DISCORD_CHARS = 1900
OUTPUT_DIR_DEFAULT = "artifacts"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_LLM_MAX_FALLBACK_MESSAGES = 200
DEFAULT_LLM_TIMEOUT_SECONDS = 20
DEFAULT_DECK_KEYWORDS = [
    "AF",
    "進化ネメシス",
    "ネメシス",
    "ロイヤル",
    "ウィッチ",
    "ドラゴン",
    "ビショップ",
    "ヴァンパイア",
    "ネクロ",
    "エルフ",
]

URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"<[@#][!&]?\d+>")
CUSTOM_EMOJI_RE = re.compile(r"<a?:[a-zA-Z0-9_]+:\d+>")
TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[ぁ-んァ-ン一-龥ー]{2,}")
KATAKANA_RE = re.compile(r"[ァ-ヴー]{2,}")
KANJI_RE = re.compile(r"[一-龥]{2,}")
MATCHUP_HINT_RE = re.compile(r"(?:vs|VS|Vs|対面)\s*[:：]?\s*([A-Za-z0-9ぁ-んァ-ン一-龥ー]+)")
RESULT_SCORE_RE = re.compile(r"(\d+)\s*[-ー]\s*(\d+)")
RESULT_JP_RE = re.compile(r"(\d+)\s*勝\s*(\d+)\s*敗")
RESULT_WL_TOKEN_RE = re.compile(r"\b([WL])\b", re.IGNORECASE)
ISSUE_LINE_RE = re.compile(r"^\s*課題\s*[:：]?\s*(.*)$")
NEXT_LINE_RE = re.compile(r"^\s*(?:次回?|next)\s*[:：]\s*(.*)$", re.IGNORECASE)
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
class RawMessage:
    message_id: str
    thread_id: str
    thread_name: str
    timestamp_utc: str
    raw_text: str


@dataclass
class StructuredEntry:
    target_date_jst: str
    message_id: str
    thread_id: str
    thread_name: str
    timestamp_utc: str
    raw_text: str
    matchup: str | None
    result: str | None
    issue: str | None
    next_action: str | None
    extract_method: str
    confidence: float
    status: str


LLMExtractor = Callable[[str], tuple[dict[str, Any] | None, str | None]]


def parse_iso8601(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def clamp_confidence(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, num))


def resolve_target_date(raw: str | None) -> date:
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    return (datetime.now(JST) - timedelta(days=1)).date()


def parse_positive_int(raw: str | None, default: int) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


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


def period_label_jst(target: date) -> str:
    period_start = datetime.combine(target, time.min, tzinfo=JST)
    period_end = period_start + timedelta(days=1) - timedelta(seconds=1)
    return (
        f"{period_start.strftime('%Y-%m-%d %H:%M:%S')} - "
        f"{period_end.strftime('%Y-%m-%d %H:%M:%S')} JST"
    )


def normalize_text(text: str) -> str:
    cleaned = URL_RE.sub(" ", text)
    cleaned = MENTION_RE.sub(" ", cleaned)
    cleaned = CUSTOM_EMOJI_RE.sub(" ", cleaned)
    return cleaned


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(text)).strip()


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
            subs = KATAKANA_RE.findall(token) + KANJI_RE.findall(token)
            if subs:
                for sub in subs:
                    add_token(sub)
                continue
            add_token(token)
    return counter.most_common(limit)


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


def make_excerpt(text: str, limit: int = 80) -> str:
    compact = compact_text(text)
    if not compact:
        return "(本文なし)"
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def categorize_llm_error(error: str | None) -> str:
    if not error:
        return "unknown error"
    lowered = error.lower()
    if "401" in lowered or "invalid_api_key" in lowered or "incorrect api key" in lowered:
        return "認証エラー (APIキー/401)"
    if "403" in lowered or "permission" in lowered or "forbidden" in lowered:
        return "権限エラー (403)"
    if "404" in lowered or ("model" in lowered and "not found" in lowered):
        return "モデル未検出/不正指定 (404)"
    if "429" in lowered or "rate limit" in lowered:
        return "レート制限 (429)"
    if "timeout" in lowered or "timed out" in lowered:
        return "タイムアウト"
    if "json" in lowered:
        return "JSON解析エラー"
    if "connection" in lowered or "network" in lowered:
        return "接続エラー"
    return error.splitlines()[0][:120]


def load_deck_keywords(path: str | None) -> list[str]:
    if not path:
        return DEFAULT_DECK_KEYWORDS[:]
    p = Path(path)
    if not p.exists():
        return DEFAULT_DECK_KEYWORDS[:]
    payload = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return DEFAULT_DECK_KEYWORDS[:]
    keywords = [str(item).strip() for item in payload if str(item).strip()]
    return keywords if keywords else DEFAULT_DECK_KEYWORDS[:]


def extract_matchup(text: str, deck_keywords: list[str]) -> str | None:
    cleaned = compact_text(text)
    if not cleaned:
        return None

    keyword_pool = sorted(set(deck_keywords), key=len, reverse=True)
    hinted_candidate: str | None = None
    hinted = MATCHUP_HINT_RE.search(cleaned)
    if hinted:
        candidate = hinted.group(1).strip()
        for keyword in keyword_pool:
            if keyword in candidate:
                return keyword
        hinted_candidate = candidate

    for keyword in keyword_pool:
        if keyword and keyword in cleaned:
            return keyword
    return hinted_candidate


def extract_result(text: str) -> str | None:
    cleaned = compact_text(text)
    if not cleaned:
        return None

    jp = RESULT_JP_RE.search(cleaned)
    if jp:
        return f"{jp.group(1)}-{jp.group(2)}"

    score = RESULT_SCORE_RE.search(cleaned)
    if score:
        return f"{score.group(1)}-{score.group(2)}"

    token = RESULT_WL_TOKEN_RE.search(cleaned)
    if token:
        return token.group(1).upper()

    if cleaned.startswith("勝") and "敗" not in cleaned:
        return "W"
    if cleaned.startswith("負") and "勝" not in cleaned:
        return "L"
    return None


def extract_issue_text(text: str) -> str | None:
    lines = normalize_text(text).splitlines() or [normalize_text(text)]
    for line in lines:
        match = ISSUE_LINE_RE.match(line)
        if not match:
            continue
        body = re.sub(r"\s+", " ", match.group(1)).strip()
        return body if body else "(詳細なし)"

    compact = compact_text(text)
    if "課題:" in compact or "課題：" in compact:
        after = re.split(r"課題[:：]", compact, maxsplit=1)[1].strip()
        return after if after else "(詳細なし)"
    return None


def extract_next_action_text(text: str) -> str | None:
    lines = normalize_text(text).splitlines() or [normalize_text(text)]
    for line in lines:
        match = NEXT_LINE_RE.match(line)
        if not match:
            continue
        body = re.sub(r"\s+", " ", match.group(1)).strip()
        if body:
            return body
    return None


def rule_extract_fields(text: str, deck_keywords: list[str]) -> dict[str, str | None]:
    return {
        "matchup": extract_matchup(text, deck_keywords),
        "result": extract_result(text),
        "issue": extract_issue_text(text),
        "next_action": extract_next_action_text(text),
    }


def has_any_structured_field(fields: dict[str, str | None]) -> bool:
    return any(fields.get(key) for key in ("matchup", "result", "issue", "next_action"))


def validate_llm_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    parsed: dict[str, Any] = {}
    for key in ("matchup", "result", "issue", "next_action", "reason_short"):
        value = payload.get(key)
        if value is None:
            parsed[key] = None
            continue
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        parsed[key] = cleaned or None
    parsed["confidence"] = clamp_confidence(payload.get("confidence", 0.0))
    return parsed


def make_openai_extractor(api_key: str, model: str, timeout_seconds: int) -> tuple[LLMExtractor | None, str | None]:
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - dependency missing path
        return None, f"OpenAI SDKの読み込みに失敗したためrule-onlyで実行: {exc}"

    client = OpenAI(api_key=api_key, timeout=timeout_seconds)

    def extractor(text: str) -> tuple[dict[str, Any] | None, str | None]:
        system_prompt = (
            "You are an extractor for Japanese game practice notes. "
            "Return strict JSON with keys: matchup,result,issue,next_action,confidence,reason_short. "
            "If unknown, use null. result must be one of: W,L,number-number,null. "
            "Keep reason_short under 30 chars."
        )
        user_prompt = (
            "以下の自由記述メモを構造化してください。\n"
            f"メモ:\n{text}\n"
            "JSONのみを返してください。"
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                return None, "LLM応答が空"
            payload = json.loads(content)
            validated = validate_llm_payload(payload)
            if not validated:
                return None, "LLM JSONの形式が不正"
            return validated, None
        except Exception as exc:  # pragma: no cover - network/external path
            return None, str(exc)

    return extractor, None


def collect_raw_messages(
    client: "DiscordClient",
    forum_channel_ids: list[str],
    target_user_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[list[RawMessage], int]:
    raw_messages: list[RawMessage] = []
    seen_message_ids: set[str] = set()
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
            for msg in messages:
                message_id = str(msg.get("id", ""))
                if not message_id or message_id in seen_message_ids:
                    continue
                author_id = str(msg.get("author", {}).get("id", ""))
                if author_id != str(target_user_id):
                    continue
                ts = str(msg.get("timestamp", ""))
                if not ts:
                    continue
                created_at = parse_iso8601(ts)
                if not (start_utc <= created_at < end_utc):
                    continue
                seen_message_ids.add(message_id)
                raw_messages.append(
                    RawMessage(
                        message_id=message_id,
                        thread_id=thread_id,
                        thread_name=thread_name,
                        timestamp_utc=ts,
                        raw_text=msg.get("content", ""),
                    )
                )

    raw_messages.sort(key=lambda x: x.timestamp_utc)
    return raw_messages, scanned_threads


def build_structured_entries(
    raw_messages: list[RawMessage],
    target: date,
    deck_keywords: list[str],
    llm_extractor: LLMExtractor | None,
    llm_max_fallback_messages: int,
) -> tuple[list[StructuredEntry], dict[str, int], dict[str, Any]]:
    entries: list[StructuredEntry] = []
    unresolved_indexes: list[int] = []

    for raw in raw_messages:
        fields = rule_extract_fields(raw.raw_text, deck_keywords)
        classified = has_any_structured_field(fields)
        entry = StructuredEntry(
            target_date_jst=target.isoformat(),
            message_id=raw.message_id,
            thread_id=raw.thread_id,
            thread_name=raw.thread_name,
            timestamp_utc=raw.timestamp_utc,
            raw_text=raw.raw_text,
            matchup=fields["matchup"],
            result=fields["result"],
            issue=fields["issue"],
            next_action=fields["next_action"],
            extract_method="rule",
            confidence=0.7 if classified else 0.0,
            status="classified" if classified else "unclassified",
        )
        entries.append(entry)
        if not classified:
            unresolved_indexes.append(len(entries) - 1)

    llm_attempted = 0
    llm_succeeded = 0
    llm_failed = 0
    llm_failure_reasons: Counter[str] = Counter()
    llm_failure_samples: list[dict[str, str]] = []
    llm_unattempted_due_limit = 0

    if llm_extractor:
        if len(unresolved_indexes) > llm_max_fallback_messages:
            llm_unattempted_due_limit = len(unresolved_indexes) - llm_max_fallback_messages
        for idx in unresolved_indexes[:llm_max_fallback_messages]:
            llm_attempted += 1
            payload, error = llm_extractor(entries[idx].raw_text)
            if not payload:
                entries[idx].extract_method = "llm_failed"
                entries[idx].status = "unclassified"
                entries[idx].confidence = 0.0
                llm_failed += 1
                reason = categorize_llm_error(error)
                llm_failure_reasons[reason] += 1
                if len(llm_failure_samples) < 5:
                    llm_failure_samples.append(
                        {
                            "message_id": entries[idx].message_id,
                            "thread_id": entries[idx].thread_id,
                            "reason": reason,
                            "raw_excerpt": make_excerpt(entries[idx].raw_text, 80),
                        }
                    )
                continue

            llm_fields = {
                "matchup": payload.get("matchup"),
                "result": payload.get("result"),
                "issue": payload.get("issue"),
                "next_action": payload.get("next_action"),
            }
            if has_any_structured_field(llm_fields):
                entries[idx].matchup = llm_fields["matchup"]
                entries[idx].result = llm_fields["result"]
                entries[idx].issue = llm_fields["issue"]
                entries[idx].next_action = llm_fields["next_action"]
                entries[idx].extract_method = "llm"
                entries[idx].status = "classified"
                entries[idx].confidence = clamp_confidence(payload.get("confidence"))
                llm_succeeded += 1
            else:
                entries[idx].extract_method = "llm_failed"
                entries[idx].status = "unclassified"
                entries[idx].confidence = 0.0
                llm_failed += 1
                reason = "LLM応答はあったが抽出項目なし"
                llm_failure_reasons[reason] += 1
                if len(llm_failure_samples) < 5:
                    llm_failure_samples.append(
                        {
                            "message_id": entries[idx].message_id,
                            "thread_id": entries[idx].thread_id,
                            "reason": reason,
                            "raw_excerpt": make_excerpt(entries[idx].raw_text, 80),
                        }
                    )

    stats = {
        "raw_count": len(raw_messages),
        "structured_count": sum(1 for e in entries if e.status == "classified"),
        "unclassified_count": sum(1 for e in entries if e.status == "unclassified"),
        "llm_attempted": llm_attempted,
        "llm_succeeded": llm_succeeded,
        "llm_failed": llm_failed,
    }
    diagnostics: dict[str, Any] = {
        "llm_failure_reasons_top": [
            {"reason": reason, "count": count}
            for reason, count in llm_failure_reasons.most_common(5)
        ],
        "llm_failure_samples": llm_failure_samples,
        "llm_unattempted_due_limit": llm_unattempted_due_limit,
    }
    return entries, stats, diagnostics


def parse_result_to_counts(result: str | None) -> tuple[int, int]:
    if not result:
        return (0, 0)
    r = result.strip().upper()
    if r == "W":
        return (1, 0)
    if r == "L":
        return (0, 1)
    score = RESULT_SCORE_RE.fullmatch(r)
    if score:
        return (int(score.group(1)), int(score.group(2)))
    jp = RESULT_JP_RE.fullmatch(r)
    if jp:
        return (int(jp.group(1)), int(jp.group(2)))
    return (0, 0)


def collect_issue_entries(entries: list[StructuredEntry]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for entry in sorted(entries, key=lambda x: x.timestamp_utc):
        if not entry.issue:
            continue
        ts_jst = parse_iso8601(entry.timestamp_utc).astimezone(JST)
        issues.append(
            {
                "thread_id": entry.thread_id,
                "thread_name": entry.thread_name,
                "time_jst": ts_jst.strftime("%H:%M"),
                "issue": entry.issue,
            }
        )
    return issues


def build_review_report(
    target: date,
    entries: list[StructuredEntry],
    warnings: list[str],
    diagnostics: dict[str, Any] | None = None,
) -> str:
    period = period_label_jst(target)
    wins = 0
    losses = 0
    for entry in entries:
        w, l = parse_result_to_counts(entry.result)
        wins += w
        losses += l

    if wins + losses > 0:
        result_label = f"{wins}勝{losses}敗 ({wins + losses}戦)"
    else:
        result_label = "算出不可（勝敗情報の抽出なし）"

    issue_counter = Counter([entry.issue for entry in entries if entry.issue])
    action_counter = Counter([entry.next_action for entry in entries if entry.next_action])
    issue_entries = collect_issue_entries(entries)
    unclassified_count = sum(1 for entry in entries if entry.status == "unclassified")

    lines = [
        f"自由記述レビュー ({target.isoformat()} JST)",
        f"- 対象期間: {period}",
        "",
        "【推定戦績】",
        f"- {result_label}",
        "",
        "【頻出課題 上位5】",
    ]

    if issue_counter:
        for idx, (issue, count) in enumerate(issue_counter.most_common(5), start=1):
            lines.append(f"{idx}. {issue} ({count})")
    else:
        lines.append("- なし")

    lines.extend(["", "【次回アクション候補 上位5】"])
    if action_counter:
        for idx, (action, count) in enumerate(action_counter.most_common(5), start=1):
            lines.append(f"{idx}. {action} ({count})")
    else:
        lines.append("1. (提案) 次: 今日の課題トップ1に対する検証手順を1つ書く")

    lines.extend(["", "【未分類メモ件数】", f"- {unclassified_count}件"])
    lines.extend(["", f"【昨日見つけた課題一覧 ({len(issue_entries)}件)】"])

    if issue_entries:
        for idx, issue in enumerate(issue_entries[:20], start=1):
            lines.append(f"{idx}. [{issue['time_jst']}] <#{issue['thread_id']}> {issue['issue']}")
        if len(issue_entries) > 20:
            lines.append(f"- ほか{len(issue_entries) - 20}件")
    else:
        lines.append("- なし")

    llm_failure_reasons = []
    if diagnostics:
        llm_failure_reasons = diagnostics.get("llm_failure_reasons_top") or []
    if llm_failure_reasons:
        lines.extend(["", "【LLM抽出失敗内訳 上位5】"])
        for item in llm_failure_reasons:
            reason = str(item.get("reason", "unknown"))
            count = int(item.get("count", 0))
            lines.append(f"- {reason}: {count}件")

    if warnings:
        lines.extend(["", "【処理メモ】"])
        for warning in warnings:
            lines.append(f"- {warning}")

    return "\n".join(lines)


def build_log_file_paths(target: date, output_dir: str) -> tuple[Path, Path, Path]:
    base = Path(output_dir)
    suffix = target.isoformat()
    raw_path = base / f"thread_user_digest_{suffix}_raw_messages.json"
    sqlite_path = base / f"thread_user_digest_{suffix}_structured.sqlite3"
    summary_path = base / f"thread_user_digest_{suffix}_summary.json"
    return raw_path, sqlite_path, summary_path


def save_raw_messages_json(
    path: Path,
    target: date,
    forum_channel_ids: list[str],
    target_user_id: str,
    raw_messages: list[RawMessage],
) -> None:
    payload = {
        "target_date": target.isoformat(),
        "period_jst": period_label_jst(target),
        "forum_channel_ids": forum_channel_ids,
        "target_user_id": target_user_id,
        "raw_count": len(raw_messages),
        "messages": [asdict(item) for item in raw_messages],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_structured_sqlite(path: Path, entries: list[StructuredEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS structured_logs (
                target_date_jst TEXT NOT NULL,
                message_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                thread_name TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                matchup TEXT,
                result TEXT,
                issue TEXT,
                next_action TEXT,
                extract_method TEXT NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO structured_logs (
                target_date_jst, message_id, thread_id, thread_name, timestamp_utc, raw_text,
                matchup, result, issue, next_action, extract_method, confidence, status
            )
            VALUES (
                :target_date_jst, :message_id, :thread_id, :thread_name, :timestamp_utc, :raw_text,
                :matchup, :result, :issue, :next_action, :extract_method, :confidence, :status
            )
            ON CONFLICT(message_id) DO UPDATE SET
                target_date_jst=excluded.target_date_jst,
                thread_id=excluded.thread_id,
                thread_name=excluded.thread_name,
                timestamp_utc=excluded.timestamp_utc,
                raw_text=excluded.raw_text,
                matchup=excluded.matchup,
                result=excluded.result,
                issue=excluded.issue,
                next_action=excluded.next_action,
                extract_method=excluded.extract_method,
                confidence=excluded.confidence,
                status=excluded.status
            """,
            [asdict(entry) for entry in entries],
        )
        conn.commit()
    finally:
        conn.close()


def save_summary_json(
    path: Path,
    target: date,
    forum_channel_ids: list[str],
    target_user_id: str,
    scanned_threads: int,
    stats: dict[str, int],
    warnings: list[str],
    diagnostics: dict[str, Any],
    report_text: str,
) -> None:
    payload = {
        "target_date": target.isoformat(),
        "period_jst": period_label_jst(target),
        "forum_channel_ids": forum_channel_ids,
        "target_user_id": target_user_id,
        "scanned_threads": scanned_threads,
        "stats": stats,
        "warnings": warnings,
        "diagnostics": diagnostics,
        "report_text": report_text,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_pipeline_outputs(
    target: date,
    forum_channel_ids: list[str],
    target_user_id: str,
    raw_messages: list[RawMessage],
    structured_entries: list[StructuredEntry],
    scanned_threads: int,
    stats: dict[str, int],
    warnings: list[str],
    diagnostics: dict[str, Any],
    report_text: str,
    output_dir: str,
) -> tuple[Path, Path, Path]:
    raw_path, sqlite_path, summary_path = build_log_file_paths(target, output_dir)
    save_raw_messages_json(raw_path, target, forum_channel_ids, target_user_id, raw_messages)
    save_structured_sqlite(sqlite_path, structured_entries)
    save_summary_json(
        summary_path,
        target,
        forum_channel_ids,
        target_user_id,
        scanned_threads,
        stats,
        warnings,
        diagnostics,
        report_text,
    )
    return raw_path, sqlite_path, summary_path


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
                "User-Agent": "thread-user-digest/2.0 (+https://github.com/<owner>/<repo>)",
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
            "User-Agent": "thread-user-digest/2.0 (+https://github.com/<owner>/<repo>)",
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
    output_dir = os.environ.get("OUTPUT_DIR", OUTPUT_DIR_DEFAULT).strip() or OUTPUT_DIR_DEFAULT
    deck_keywords_path = os.environ.get("DECK_KEYWORDS_PATH")
    llm_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    llm_model = os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL
    llm_max = parse_positive_int(
        os.environ.get("LLM_MAX_FALLBACK_MESSAGES"),
        DEFAULT_LLM_MAX_FALLBACK_MESSAGES,
    )
    llm_timeout = parse_positive_int(
        os.environ.get("LLM_TIMEOUT_SECONDS"),
        DEFAULT_LLM_TIMEOUT_SECONDS,
    )

    target = resolve_target_date(target_date_raw)
    start_utc, end_utc = day_bounds_utc(target)
    warnings: list[str] = []

    deck_keywords = load_deck_keywords(deck_keywords_path)

    llm_extractor: LLMExtractor | None = None
    if llm_api_key:
        llm_extractor, llm_warning = make_openai_extractor(llm_api_key, llm_model, llm_timeout)
        if llm_warning:
            warnings.append(llm_warning)
            llm_extractor = None
    else:
        warnings.append("OPENAI_API_KEY未設定のためrule-only modeで実行")

    client = DiscordClient(bot_token)
    raw_messages, scanned_threads = collect_raw_messages(
        client,
        forum_channel_ids,
        target_user_id,
        start_utc,
        end_utc,
    )

    structured_entries, stats, diagnostics = build_structured_entries(
        raw_messages,
        target,
        deck_keywords,
        llm_extractor,
        llm_max,
    )
    if stats["llm_failed"] > 0:
        warnings.append(f"LLM抽出失敗: {stats['llm_failed']}件")
        for item in diagnostics.get("llm_failure_reasons_top", [])[:3]:
            warnings.append(f"LLM失敗内訳: {item['reason']} ({item['count']}件)")
    if diagnostics.get("llm_unattempted_due_limit", 0) > 0:
        warnings.append(f"LLM未実行（上限超過）: {diagnostics['llm_unattempted_due_limit']}件")

    report = build_review_report(target, structured_entries, warnings, diagnostics)
    print(report)

    raw_path, sqlite_path, summary_path = save_pipeline_outputs(
        target=target,
        forum_channel_ids=forum_channel_ids,
        target_user_id=target_user_id,
        raw_messages=raw_messages,
        structured_entries=structured_entries,
        scanned_threads=scanned_threads,
        stats=stats,
        warnings=warnings,
        diagnostics=diagnostics,
        report_text=report,
        output_dir=output_dir,
    )
    print(f"ログ保存: {raw_path} / {sqlite_path} / {summary_path}")

    if dry_run:
        print("DRY_RUN=true のため投稿をスキップしました。")
        return

    for chunk in split_for_discord(report):
        post_to_webhook(webhook_url, report_thread_id, chunk)
    print("Discord へのレポート投稿が完了しました。")


if __name__ == "__main__":
    run()
