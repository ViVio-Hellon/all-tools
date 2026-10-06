"""起動待機画面 (基盤仕様書 2.2 / 2.3)

【なぜ独立したモジュールなのか】
この画面は **Flask が読み込まれる前に**出さなければならない。起動で
いちばん時間を食うのは Flask とアプリ本体の import で、そのあとに
待機画面を出していたら「待たせるために見せるもの」を待たせている
ことになる ── 出す意味がない。

そこでここは **標準ライブラリだけ**で組み、`boot_server` が最小の WSGI
で先に出せるようにしてある。本体が組み上がったあとも同じ関数を Flask
側の `/` が呼ぶので、**骨格は1か所**しかない(2枚持つと、直したほうと
直していないほうが場面によって出る)。

【1往復で出す】
外部への要求(CSS・画像・書体)を1つも出さない。色は
`app/static/css/tokens.css` を読んで**そのまま埋め込む** ── 色の
出どころを2つにしないための読み込みで、失敗しても手元の控えで描ける。

【段の見せ方】
基盤仕様書 2.2 は「進捗率を正確に計算するための機能ではない」と
断っている。細かくせず4段だけを出し、失敗したときは**次に何を確認
すればよいか**(ログの場所)を出す。
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Optional

# `tokens.css` の場所。**色の出どころはここ1つ**
_TOKENS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "tokens.css"

# `tokens.css` を読めなかったときの控え。起動画面が出ないよりは、
# 色が少し違ってでも出るほうがよい
_FALLBACK_TOKENS = """
:root{
  --bar-a:#1b3a2f; --bar-b:#2f7d5f; --bar-ink:#ffffff; --bar-muted:#c8e3d6;
  --bar-face:#ffffff14; --bar-edge:#ffffff33; --bar-focus:#7fe3c0;
  --bar-ok:#8ff0c4; --bar-alert:#ffc4bf;
  --sans:"Yu Gothic UI","Meiryo UI",system-ui,sans-serif;
  --mono:ui-monospace,Consolas,"Courier New",monospace;
}
"""

# 出す段。**キーで判断し、文言は表示のためだけに使う**
# (文言から段を推し量ると、文言を直した日に判定が壊れる)
STAGES: tuple[tuple[str, str], ...] = (
    ("env", "実行環境を確認中"),
    ("prepare", "アプリを準備中"),
    ("connect", "接続を確認中"),
    ("done", "起動完了"),
)

_tokens_cache: Optional[str] = None


def tokens_css() -> str:
    """色の定義。1度読んで覚える(起動のたびに何度も読まない)。"""
    global _tokens_cache
    if _tokens_cache is None:
        try:
            _tokens_cache = _TOKENS.read_text(encoding="utf-8")
        except OSError:
            _tokens_cache = _FALLBACK_TOKENS
    return _tokens_cache


def render(*, display_name: str, version_label: str, token: str,
           app_id: str, poll_ms: int, home_url: str,
           log_dir: str = "") -> str:
    """待機画面のHTMLを組み立てる。

    `poll_ms` の間隔で `/api/health` を見に行き、`ready` になったら
    `home_url` へ移る。**一定秒数の経過では完了扱いにしない**
    (基盤仕様書 2.3「バックエンドの応答を確認して画面を切り替える」)。
    """
    stages_html = "".join(
        f'<li data-key="{html.escape(key)}"><span class="dot"></span>'
        f'<span class="t">{html.escape(label)}</span></li>'
        for key, label in STAGES
    )
    config = json.dumps({
        "token": token,
        "appId": app_id,
        "pollMs": max(150, int(poll_ms)),
        "homeUrl": home_url,
        "stages": [key for key, _ in STAGES],
    }, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(display_name)} を起動しています</title>
<style>
{tokens_css()}
*{{box-sizing:border-box}}
html,body{{height:100%;margin:0}}
body{{
  font-family:var(--sans);color:var(--bar-ink);
  background:linear-gradient(135deg,var(--bar-a),var(--bar-b));
  display:grid;place-items:center;padding:24px;
}}
.card{{width:min(560px,100%);text-align:center}}
h1{{font-size:20px;margin:0 0 4px;font-weight:700}}
.ver{{font-size:12px;color:var(--bar-muted);font-family:var(--mono)}}
.track{{
  height:6px;border-radius:99px;background:var(--bar-face);
  overflow:hidden;margin:24px 0 16px;
}}
.fill{{
  height:100%;width:12%;border-radius:99px;background:var(--bar-focus);
  transition:width .4s ease;
}}
ol{{
  list-style:none;display:flex;justify-content:space-between;
  gap:8px;padding:0;margin:0;
}}
ol li{{
  flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;
  font-size:12px;color:var(--bar-muted);
}}
ol li .dot{{
  width:10px;height:10px;border-radius:50%;
  background:var(--bar-face);border:1px solid var(--bar-edge);
}}
ol li[data-state="doing"]{{color:var(--bar-ink);font-weight:700}}
ol li[data-state="doing"] .dot{{background:var(--bar-focus);border-color:var(--bar-focus)}}
ol li[data-state="done"] .dot{{background:var(--bar-ok);border-color:var(--bar-ok)}}
.note{{margin-top:20px;font-size:13px;color:var(--bar-muted);min-height:1.4em}}
.elapsed{{font-family:var(--mono);font-size:12px;color:var(--bar-muted);margin-top:6px}}
.error{{
  margin-top:20px;padding:12px 14px;border-radius:8px;text-align:left;
  background:#00000033;border:1px solid var(--bar-alert);
  color:var(--bar-alert);font-size:13px;line-height:1.7;white-space:pre-wrap;
}}
.error b{{display:block;margin-bottom:4px}}
.enter{{
  margin-top:18px;display:inline-block;padding:8px 18px;border-radius:6px;
  background:var(--bar-face);border:1px solid var(--bar-edge);
  color:var(--bar-ink);text-decoration:none;font-size:13px;
}}
.enter:hover{{background:var(--bar-edge)}}
[hidden]{{display:none !important}}
</style>
</head>
<body>
<main class="card">
  <h1>{html.escape(display_name)}</h1>
  <div class="ver">{html.escape(version_label)}</div>

  <div class="track"><div class="fill" id="fill"></div></div>
  <ol id="stages">{stages_html}</ol>

  <p class="note" id="note">起動しています…</p>
  <p class="elapsed" id="elapsed"></p>

  <div class="error" id="error" hidden>
    <b>起動できませんでした</b>
    <span id="errtext"></span>
  </div>

  <a class="enter" id="enter" href="{html.escape(home_url)}" hidden>そのまま画面へ入る</a>
</main>

<script>
const CONF = {config};
const LOG_DIR = {json.dumps(log_dir, ensure_ascii=False)};
const started = Date.now();
const fill = document.getElementById("fill");
const note = document.getElementById("note");
const elapsed = document.getElementById("elapsed");
const errorBox = document.getElementById("error");
const errorText = document.getElementById("errtext");
const enter = document.getElementById("enter");
const items = [...document.querySelectorAll("#stages li")];

// 経過時間。**無反応に見える時間をなくす**のが待機画面の目的なので、
// 応答が来ていなくてもここは動き続ける
setInterval(() => {{
  const sec = Math.floor((Date.now() - started) / 1000);
  elapsed.textContent = sec > 0 ? `経過 ${{sec}} 秒` : "";
}}, 500);

/** 段を塗る。キーで判断する(文言では判断しない)。 */
function paintStage(key) {{
  const at = Math.max(0, CONF.stages.indexOf(key));
  items.forEach((li, i) => {{
    li.dataset.state = i < at ? "done" : (i === at ? "doing" : "");
  }});
  fill.style.width = `${{Math.round(((at + 1) / CONF.stages.length) * 100)}}%`;
}}

function showError(message) {{
  errorText.textContent = message + (LOG_DIR ? `\\n\\nログ: ${{LOG_DIR}}` : "");
  errorBox.hidden = false;
  note.textContent = "";
}}

async function poll() {{
  try {{
    const res = await fetch("/api/health", {{cache: "no-store"}});
    if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
    const body = await res.json();

    // **同じアプリか確かめる**(基盤仕様書 2.3)。同じポートを別の
    // アプリが使っていても、HTTPが返るだけでは起動成功とは言えない
    if (body.app_id && body.app_id !== CONF.appId) {{
      showError("このポートで別のアプリが動いています: " + body.app_id);
      return;
    }}

    if (body.startup_error) {{
      showError(body.startup_error);
      enter.hidden = false;
      return;
    }}

    paintStage(body.stage_key || "prepare");
    // いま走っているものがあれば、それを出す。取り込みは数分かかる
    // ことがあり、出さないと「止まっている」と受け取られる
    const job = body.job;
    note.textContent = job
      ? `${{job.label}}${{job.message ? " — " + job.message : ""}}`
      : (body.stage || "起動しています…");

    if (body.ready) {{
      paintStage("done");
      note.textContent = "起動しました。画面へ移ります…";
      location.replace(body.home_url || CONF.homeUrl);
      return;
    }}
  }} catch (err) {{
    // まだ待ち受けが始まっていないだけのことが多い。黙って待つ
    note.textContent = "起動しています…";
  }}
  setTimeout(poll, CONF.pollMs);
}}

// 30秒たっても終わらなければ、待たずに入る道を出す
setTimeout(() => {{ enter.hidden = false; }}, 30000);

paintStage("env");
poll();
</script>
</body>
</html>
"""
