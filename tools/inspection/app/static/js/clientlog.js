/*
  clientlog.js — 画面(ブラウザ)の中で起きたことをサーバのログに残す

  画面の中の失敗は、これまでどこにも残らず後追いできなかった。
  ・スクリプトのエラー・取りこぼした失敗 → すぐ送る
  ・サーバにつながらなかった → **届かないので覚えておき、つながったら**送る
    (何秒・何回・どの操作で。「画面が止まった」と言われたときの手がかり)
  サーバが断った・失敗したもの(4xx/5xx)はサーバが記録済みなので送らない。
  送れなくても画面は止めない。送りすぎない(サーバ側でも1画面 10分 30件まで)。
*/

const offline = { since: 0, count: 0, paths: new Set() };
let sent = 0;
const MAX_PER_PAGE = 50;

function send(kind, payload) {
  if (sent >= MAX_PER_PAGE || !window.APP) return;
  sent += 1;
  try {
    fetch("/api/client-log", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Tool-Token": window.APP.token,
                 "X-Screen-Id": window.APP.screenId || "" },
      body: JSON.stringify({ kind, page: location.pathname, ...payload }),
    }).catch(() => { /* 届かなければそれまで */ });
  } catch { /* 送れなくても画面は続ける */ }
}

/** api.js から: サーバにつながらなかった。 */
export function noteOffline(path) {
  if (!offline.since) offline.since = Date.now();
  offline.count += 1;
  if (offline.paths.size < 10) offline.paths.add(path.split("?")[0]);
}

/** health.js から: つながり直した。切れていたあいだのことを残す。 */
export function flushOffline() {
  if (!offline.since) return;
  const seconds = Math.round((Date.now() - offline.since) / 1000);
  send("offline", {
    message: `サーバーにつながらなかった(約 ${seconds} 秒・${offline.count} 回)`,
    detail: `切れ始め: ${new Date(offline.since).toLocaleString("ja-JP")}\n届かなかった宛先: ${[...offline.paths].join(", ")}`,
  });
  offline.since = 0;
  offline.count = 0;
  offline.paths.clear();
}

export function install() {
  window.addEventListener("error", (ev) => {
    send("js_error", {
      message: String(ev.message || "スクリプトのエラー"),
      detail: `${ev.filename || ""}:${ev.lineno || 0}:${ev.colno || 0}\n${(ev.error && ev.error.stack) || ""}`,
    });
  });
  window.addEventListener("unhandledrejection", (ev) => {
    const err = ev.reason || {};
    if (err.name === "ApiError" && err.status > 0) return;   // サーバが記録済み
    if (err.name === "ApiError" && err.status === 0) return;  // つながったら flushOffline で残す
    send("unhandled", { message: String(err.message || err), detail: String(err.stack || "") });
  });
}
