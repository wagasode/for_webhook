import os, json, urllib.request

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
THREAD_ID = os.environ["DISCORD_THREAD_ID"]
POST_URL = f"{WEBHOOK_URL}?thread_id={THREAD_ID}"

CONTENT = r"""# 週次数値目標：〜12/21(日)
Level1: Eクラス 1900
Level2: ERWどれかで1950
Level3: 3クラスで1950
---順調にいったらここまでは行けそう
Level4: どれか2000
---行けたらいいね
Level5: 3クラス2000
---行けたらガチ嬉しい
Level6: どれか2050
---神
Level7: 3クラス2050
Level8: どれか2100

# 今週の練習方針
- CR盛るために夜更かしするの禁止
- 勝てなくてイライラした状態でマッチングボタン連打するの禁止
目的：プレイが下手なのは許せるけど、メンタル管理が下手なのは許し難いので、鍛える。

### 具体的なルール
- 23:30以降のランクマッチ禁止
  - 対戦以外は全てok（座学とか）
- 戦績管理スプシに「イライラしたか？」項目を設置
  - した、ちょいした、しなかったの3項目
  - 「した」2連もしくは「ちょいした以上」3連でブロック終了

### 日報のチェック項目
- ①23:30前にランクマ終了したか
- ②ブロック終了条件遵守率（1h継続・2連敗・連続tiltの全条件を含む）
- ③その日の「イライラしたか？」項目の平均値（してない0~2した）
- ④その日の「形勢判断意識」項目の平均値（全く意識できなかった0~5超意識できた）
```
  |①|②|③|④|
--------------
15|　|　|　|　|
16|　|　|　|　|
17|　|　|　|　|
18|　|　|　|　|
19|　|　|　|　|
20|　|　|　|　|
21|　|　|　|　|
--------------
```
"""

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
if len(CONTENT) <= 2000:
    post(CONTENT)
else:
    buf = ""
    for line in CONTENT.splitlines(True):
        if len(buf) + len(line) > MAX:
            post(buf)
            buf = ""
        buf += line
    if buf:
        post(buf)