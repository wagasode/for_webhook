import os, json, urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo
MODE = os.environ.get("MODE", "morning")
USER_ID = os.environ["DISCORD_USER_ID"]
JST = ZoneInfo("Asia/Tokyo")
today_str = __import__("datetime").datetime.now(JST).strftime("%Y-%m-%d")
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
THREAD_ID = os.environ["DISCORD_THREAD_ID"]
POST_URL = f"{WEBHOOK_URL}?thread_id={THREAD_ID}"

SCRIPT_DIR = Path(__file__).resolve().parent


def load_message(name: str) -> str:
    path = SCRIPT_DIR / "messages" / f"{name}.txt"
    return path.read_text(encoding="utf-8")


CONTENT = load_message("morning")
NIGHT_TEMPLATE = load_message("night")
NIGHT = NIGHT_TEMPLATE.format(user_id=USER_ID, today=today_str)

def post(text: str):
    payload = json.dumps({"content": text}).encode("utf-8")
    req = urllib.request.Request(
        POST_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "nippo-reminder/1.0 (+https://github.com/<owner>/<repo>)",
            },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()

# 2000文字制限対策（安全側で分割）
MAX = 1900
text = CONTENT if MODE == "morning" else NIGHT
if len(text) <= 2000:
    post(text)
else:
    buf = ""
    for line in text.splitlines(True):
        if len(buf) + len(line) > MAX:
            post(buf)
            buf = ""
        buf += line
    if buf:
        post(buf)
