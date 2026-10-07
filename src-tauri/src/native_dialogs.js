// 画面の alert / confirm / prompt を、ブラウザ(WebView2・WebKitGTK)のものに固定する。
//
// tauri-plugin-dialog は、どの画面の window.confirm も「答えを待たない」版(async。
// Promise を返す)に置き換える。すると `if (!confirm("消しますか?")) return;` は
// Promise を「はい」とみなし、**訊かずに進む**(消す・発送・終了…)。Windows では
// この置き換えが各ツールの画面(iframe)にも入る(WebView2 は初期化の台本を全部の枠に入れる)。
//
// この台本は dialog より先に全部の枠で走り、ブラウザのものを書き換えられないようにする。
// dialog の台本("use strict")は代入で TypeError になって止まり、ブラウザのものが残る。
(function () {
  ["alert", "confirm", "prompt"].forEach(function (name) {
    var native = window[name];
    if (typeof native !== "function") return;
    try {
      Object.defineProperty(window, name, {
        value: native, writable: false, configurable: false, enumerable: true,
      });
    } catch (e) { /* もう固定されている */ }
  });
})();
