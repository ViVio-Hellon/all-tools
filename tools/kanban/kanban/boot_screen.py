"""起動待機画面の HTML (基盤仕様書 2.2 / 2.3)

【なぜ要るのか】
Python モジュールの読込・設定確認・SQLite の初期化・Access からの取り込みは
数秒から数十秒かかる。その間まっ白なら、利用者には「動いていない」としか
見えない。押し直され、二重に起動され、電話がかかってくる。

【この画面が守っていること】

1. **外部への要求を出さない。** CSS も JS もこの1枚に埋め込む。まだ本体が
   立ち上がっていないので、``/static/`` を取りに行っても返ってこない
2. **一定秒数で完了扱いにしない。** ``/api/health`` の ``ready`` を見て
   から切り替える(基盤仕様書 2.3)。時間で切り替えると、遅い端末で
   「接続できません」の画面に飛ばすことになる
3. **失敗したら次の行動を出す。** どこを見ればよいか(ログの場所)まで
   書く。「起動できませんでした」だけでは現場は手の打ちようがない
"""

from __future__ import annotations

import html

#: 起動の段。細かくしすぎない(基盤仕様書 2.2)
STAGES: tuple[tuple[str, str], ...] = (
    ("env", "実行環境を確認中"),
    ("prepare", "アプリを準備中"),
    ("connect", "接続を確認中"),
    ("done", "起動完了"),
)

_PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{display_name} を起動しています</title>
<script>
  // 画面の明るさ。業務画面と同じ選び方(帯の「◐ 自動 / ☀ / ☾」)に合わせる
  (function () {{
    var c = 'auto';
    try {{ c = localStorage.getItem('kanban.theme') || 'auto'; }} catch (e) {{}}
    var dark = c === 'dark' || (c === 'auto' && window.matchMedia
      && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  }})();
</script>
<style>
  :root {{
    --bg: #f4f6f8; --fg: #1b1f23; --muted: #5b6672;
    --line: #d7dde3; --accent: #0f6fc5; --bad: #b3261e;
    --card: #ffffff; --code-bg: #eef1f4; --bad-bg: #fdf0ef; --bad-line: #f2c8c4;
    color-scheme: light;
  }}
  :root[data-theme="dark"] {{
    --bg: #15191d; --fg: #e4e8ec; --muted: #9ba6b2;
    --line: #353d45; --accent: #5aa3ec; --bad: #f0716a;
    --card: #1e2328; --code-bg: #262c32; --bad-bg: #3a1f1e; --bad-line: #6b2f2b;
    color-scheme: dark;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: var(--bg); color: var(--fg);
    font-family: "Meiryo UI", "Yu Gothic UI", system-ui, sans-serif;
  }}
  .card {{
    width: min(30rem, calc(100vw - 2rem));
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 1.75rem 1.75rem 1.5rem; box-shadow: 0 2px 12px rgba(0,0,0,.06);
  }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.15rem; }}
  .ver {{ color: var(--muted); font-size: .8rem; }}
  .stages {{ margin: 1.25rem 0 0; padding: 0; list-style: none; }}
  .stages li {{
    display: flex; align-items: center; gap: .6rem;
    padding: .35rem 0; color: var(--muted); font-size: .9rem;
  }}
  .dot {{
    width: .7rem; height: .7rem; border-radius: 50%;
    border: 2px solid var(--line); flex: none;
  }}
  li[data-state="active"] {{ color: var(--fg); font-weight: 600; }}
  li[data-state="active"] .dot {{
    border-color: var(--accent);
    animation: pulse 1s ease-in-out infinite;
  }}
  li[data-state="done"] {{ color: var(--fg); }}
  li[data-state="done"] .dot {{
    border-color: var(--accent); background: var(--accent);
  }}
  @keyframes pulse {{ 50% {{ opacity: .3; }} }}
  @media (prefers-reduced-motion: reduce) {{
    li[data-state="active"] .dot {{ animation: none; }}
  }}
  .elapsed {{ margin-top: 1rem; color: var(--muted); font-size: .8rem; }}
  .error {{
    margin-top: 1rem; padding: .75rem .9rem; border-radius: 6px;
    background: var(--bad-bg); border: 1px solid var(--bad-line); color: var(--bad);
    font-size: .85rem; line-height: 1.6; white-space: pre-wrap;
  }}
  .error[hidden] {{ display: none; }}
  .error b {{ display: block; margin-bottom: .3rem; }}
  code {{
    background: var(--code-bg); padding: .1rem .3rem; border-radius: 3px;
    font-size: .95em; word-break: break-all;
  }}
</style>
</head>
<body>
<div class="card">
  <h1>{display_name}</h1>
  <div class="ver">{version_label} — 起動しています</div>

  <ul class="stages" id="stages">
{stage_items}
  </ul>

  <div class="elapsed" id="elapsed">経過 0 秒</div>

  <div class="error" id="error" hidden>
    <b>起動できませんでした</b>
    <span id="errtext"></span>
  </div>
</div>

<script>
(function () {{
  "use strict";
  var TOKEN = {token_js};
  var APP_ID = {app_id_js};
  var HOME = {home_js};
  var POLL = {poll_ms};
  var ORDER = {order_js};
  var started = Date.now();

  var elapsed = document.getElementById("elapsed");
  var errBox = document.getElementById("error");
  var errText = document.getElementById("errtext");

  setInterval(function () {{
    elapsed.textContent = "経過 " + Math.floor((Date.now() - started) / 1000) + " 秒";
  }}, 1000);

  function paint(stageKey) {{
    var at = ORDER.indexOf(stageKey);
    if (at < 0) at = 0;
    ORDER.forEach(function (key, i) {{
      var li = document.querySelector('li[data-key="' + key + '"]');
      if (!li) return;
      li.dataset.state = i < at ? "done" : (i === at ? "active" : "");
    }});
  }}

  function fail(message) {{
    errText.textContent = message;
    errBox.hidden = false;
  }}

  function tick() {{
    fetch("/api/health", {{ cache: "no-store" }})
      .then(function (r) {{ return r.ok ? r.json() : null; }})
      .then(function (h) {{
        if (!h) return;
        // 同じポートに別のアプリが居ることがある(基盤仕様書 2.3)。
        // app_id を確かめてから「起動した」と判断する
        if (h.app_id !== APP_ID) {{
          fail("このポートでは別のアプリが動いています (" +
               (h.display_name || h.app_id || "不明") + ")。\\n" +
               "一度すべて終了してから開き直してください。");
          return;
        }}
        if (h.startup_error) {{ fail(h.startup_error); return; }}
        paint(h.stage_key || "prepare");
        if (h.ready) {{
          // トークンは URL で渡す。ここで初めて本体の画面へ入る
          location.replace(HOME + (HOME.indexOf("?") < 0 ? "?" : "&") +
                           "t=" + encodeURIComponent(TOKEN));
        }}
      }})
      .catch(function () {{ /* まだ立ち上がっていないだけ。次の周期で試す */ }});
  }}

  tick();
  setInterval(tick, POLL);
}}());
</script>
</body>
</html>
"""

_ITEM = '    <li data-key="{key}" data-state=""><span class="dot"></span>{label}</li>'


def render(
    *,
    display_name: str,
    version_label: str,
    token: str,
    app_id: str,
    poll_ms: int = 300,
    home_url: str = "/",
) -> str:
    """待機画面を1枚組み立てる。

    値はすべて呼び出し側(``boot_server``)が渡す。この関数は
    ``app_config`` を読まない ── 起動でいちばん早く動く部品なので、
    import を1つでも減らしておく。
    """
    import json

    items = "\n".join(_ITEM.format(key=key, label=html.escape(label)) for key, label in STAGES)
    return _PAGE.format(
        display_name=html.escape(display_name),
        version_label=html.escape(version_label),
        stage_items=items,
        token_js=json.dumps(token),
        app_id_js=json.dumps(app_id),
        home_js=json.dumps(home_url),
        poll_ms=int(poll_ms),
        order_js=json.dumps([key for key, _ in STAGES]),
    )
