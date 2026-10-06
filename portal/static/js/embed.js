/*
  embed.js — 統合ツールの外枠(Rust)が、各ツールの画面(HTML)の先頭に差し込む台本

  **各ツールの画面は書き換えない。** 各ツールの desktop.js は `window.__TAURI__` を見て、
  別窓・保存・フォルダの選択・立て直しを外枠に頼む。大きなタブの中(iframe)では
  Tauri の口が無い(Linux)/ OS によって入り方が違う(Windows は子の枠にも入る)ので、
  ここで `window.__TAURI__` を用意し、頼みごとを大きなタブの画面へ `postMessage` で渡す。
  大きなタブの画面が送り元(枠と宛先)を確かめてから外枠へ取り次ぐ(portal/static/js/shell.js)。

  別窓(帳票・印刷・VC 早見表)は大きなタブの外の窓なので、外枠へ直に頼む。

  あわせて、1つの窓に並べたための手当て:
  ・F5 / Ctrl+R … **このツールの画面だけ**を読み直す(窓ごと読み直すと全タブが消える)
  ・Ctrl+P      … このツールの画面を印刷する(ツールが自分で受けていればそちら)
  ・Alt+1〜9    … 大きなタブを切り替える
  ・アプリの外へのリンク … 既定のブラウザで開く(窓の中で開かない)

  `"__ALLTOOLS_TOOL__"` と `"__ALLTOOLS_SHELL__"` は、外枠が差し込むときに
  ツールの名前と大きなタブの画面の宛先に置き換える(`src-tauri/src/shell.rs`)。
*/
(function () {
  "use strict";
  if (window.__ALLTOOLS__) return;
  var TOOL = "__ALLTOOLS_TOOL__";
  var SHELL = "__ALLTOOLS_SHELL__";
  var internals = window.__TAURI_INTERNALS__;
  var parentWin = window.parent;
  var embedded = parentWin && parentWin !== window;
  var sameOriginParent = false;
  if (embedded) {
    try { sameOriginParent = Boolean(parentWin.__ALLTOOLS__) && parentWin.location.origin === location.origin; }
    catch (e) { sameOriginParent = false; }
  }

  var pending = {};
  var seq = 0;

  function viaShell(cmd, args, options) {
    return new Promise(function (resolve, reject) {
      seq += 1;
      var id = seq;
      pending[id] = { resolve: resolve, reject: reject };
      var raw = (args instanceof Uint8Array || args instanceof ArrayBuffer) ? args : null;
      var msg = { type: "alltools:invoke", id: id, cmd: String(cmd),
                  args: raw ? null : (args === undefined ? null : args),
                  raw: raw, headers: raw && options && options.headers ? options.headers : null };
      try {
        parentWin.postMessage(msg, SHELL);
      } catch (e) {
        delete pending[id];
        reject(e);
      }
    });
  }

  function direct(cmd, args, options) {
    if (args instanceof Uint8Array || args instanceof ArrayBuffer) {
      var headers = {};
      var given = (options && options.headers) || {};
      for (var k in given) { if (Object.prototype.hasOwnProperty.call(given, k)) headers[k] = given[k]; }
      headers["x-alltools-tool"] = TOOL;
      headers["x-alltools-cmd"] = String(cmd);
      return internals.invoke("tool_invoke_raw", args, { headers: headers });
    }
    return internals.invoke("tool_invoke", { tool: TOOL, cmd: String(cmd), args: args || {} });
  }

  var invoke = null;
  if (embedded && sameOriginParent) {
    // ツールの画面の中の枠(VC のコイル計算など)。外側の画面の口を使う
    invoke = function (cmd, args, options) { return parentWin.__TAURI__.core.invoke(cmd, args, options); };
  } else if (embedded) {
    invoke = viaShell;
  } else if (internals && internals.invoke) {
    invoke = direct;
  }

  window.addEventListener("message", function (event) {
    if (event.origin !== SHELL || event.source !== parentWin) return;
    var d = event.data;
    if (!d || d.type !== "alltools:result" || !pending[d.id]) return;
    var p = pending[d.id];
    delete pending[d.id];
    if (d.ok) p.resolve(d.value); else p.reject(new Error(String(d.value)));
  });

  if (invoke) {
    var api = { core: { invoke: invoke } };
    try {
      Object.defineProperty(window, "__TAURI__", { value: api, configurable: true, writable: true });
    } catch (e) {
      window.__TAURI__ = api;
    }
  }
  window.__ALLTOOLS__ = { tool: TOOL, shell: SHELL, embedded: embedded };

  function tellShell(data, type) {
    if (!embedded || sameOriginParent) return;
    data.type = type || "alltools:key";
    try { parentWin.postMessage(data, SHELL); } catch (e) { /* 大きなタブが居ない */ }
  }

  // ---- 画面が出た(大きなタブの画面が記録に残す。外枠の「起動しています」とは別) ----
  function ready() {
    tellShell({ title: String(document.title || ""), path: location.pathname }, "alltools:ready");
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ready);
  } else {
    ready();
  }

  // ---- キー(このツールの画面にいるとき) ----
  window.addEventListener("keydown", function (event) {
    if (event.defaultPrevented) return;
    var key = event.key || "";
    var ctrl = event.ctrlKey || event.metaKey;
    if (key === "F5" || (ctrl && (key === "r" || key === "R"))) {
      event.preventDefault();
      location.reload();
      return;
    }
    if (ctrl && !event.shiftKey && !event.altKey && (key === "p" || key === "P")) {
      // ツールが自分で受けていない Ctrl+P。窓ごとではなく、この画面を刷る
      event.preventDefault();
      window.print();
      return;
    }
    if (event.altKey && !ctrl && /^[1-9]$/.test(key)) {
      event.preventDefault();
      tellShell({ action: "tab", index: Number(key) - 1 });
    }
  });

  // ---- アプリの外へのリンク(既定のブラウザで開く) ----
  // `window` で受ける(各ツールの `document` の見張りより後に回る)。ツールが自分で
  // 開いた(`preventDefault` 済み)リンクには触らない ── 2回開かない
  window.addEventListener("click", function (event) {
    if (event.defaultPrevented || !invoke) return;
    var link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!link || link.hasAttribute("download")) return;
    var url;
    try { url = new URL(link.href, location.href); } catch (e) { return; }
    if (url.origin === location.origin) return;
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    event.preventDefault();
    invoke("open_external", { url: url.href }).catch(function () {});
  });
})();
