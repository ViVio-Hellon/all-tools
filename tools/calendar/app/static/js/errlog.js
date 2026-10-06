/*
  errlog.js — 画面で起きたエラーをサーバのログへ送る

  **画面のエラーは、送らないと誰も知らない。** JS の例外はブラウザの
  開発者ツールにしか出ず、現場には「押しても何も起きない」としか見えない。
  サーバが記録番号を付けて残し(`app/routes/diagnostics.py`)、
  その番号を画面にも出す(`app.js` が `app:client-error` を受けて出す)。

  **モジュールにしない。** `app.js` 側の読み込みが失敗した(構文の誤り・
  ファイルが無い)ときこそ残したいので、先に普通のスクリプトとして読み、
  見張りを立てておく。

  送りすぎない: 1枚の画面で10件まで・同じ文言は1回だけ。
*/
(function () {
  "use strict";
  var LIMIT = 10;
  var sent = 0;
  var seen = {};
  var app = window.APP || {};

  function screenId() {
    try { return sessionStorage.getItem("screenId") || ""; } catch (e) { return ""; }
  }

  function report(kind, message, source, line, col, stack) {
    var text = String(message || "(文言なし)");
    if (seen[text] || sent >= LIMIT) return;
    seen[text] = true;
    sent += 1;
    var body = {
      kind: kind, message: text, source: String(source || ""),
      line: line || "", col: col || "", stack: String(stack || ""),
      page: location.pathname + location.search.replace(/([?&])t=[^&]*/, "$1t=***"),
      screen: screenId()
    };
    try {
      fetch("/api/client-log", {
        method: "POST",
        cache: "no-store",
        keepalive: true,
        headers: { "Content-Type": "application/json", "X-Tool-Token": app.token || "" },
        body: JSON.stringify(body)
      }).then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) {
          if (data && data.ref) {
            document.dispatchEvent(new CustomEvent("app:client-error",
              { detail: { ref: data.ref, message: text } }));
          }
        })
        .catch(function () { /* 届かなければそれまで(サーバが止まっている) */ });
    } catch (e) { /* 送れなくても画面を止めない */ }
  }

  window.addEventListener("error", function (event) {
    // 画像などの読み込み失敗(event.error が無く target が要素)は除く
    if (event.target && event.target !== window && !event.message) return;
    var err = event.error;
    report("error", event.message || (err && err.message), event.filename,
           event.lineno, event.colno, err && err.stack);
  }, true);

  window.addEventListener("unhandledrejection", function (event) {
    var reason = event.reason;
    // **サーバの断り(ApiError)は送らない。** 断りの理由はサーバが
    // 既に残している(`app/tracing.py`)
    if (reason && reason.name === "ApiError") return;
    report("rejection", (reason && reason.message) || String(reason),
           "", "", "", reason && reason.stack);
  });
})();
