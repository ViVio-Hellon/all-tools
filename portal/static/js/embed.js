/*
  embed.js — 統合ツールの外枠(Rust)が、各ツールの画面(HTML)の先頭に差し込む台本

  **各ツールの画面は書き換えない。** 各ツールの desktop.js は `window.__TAURI__` を見て、
  別窓・保存・フォルダの選択・立て直しを外枠に頼む。大きなタブの中(iframe)では
  Tauri の口が無い(Linux)/ OS によって入り方が違う(Windows は子の枠にも入る)ので、
  ここで `window.__TAURI__` を用意し、頼みごとを大きなタブの画面へ `postMessage` で渡す。
  大きなタブの画面が送り元(枠と宛先)を確かめてから外枠へ取り次ぐ(portal/static/js/shell.js)。

  別窓(帳票・印刷・VC 早見表)は大きなタブの外の窓なので、外枠へ直に頼む。

  あわせて、1つの窓に並べたための手当て:
  ・F5 / Ctrl+R … **このツールの画面だけ**を読み直す(窓ごと読み直すと全タブが消える)。
                  読み直すのは大きなタブの画面に頼む(先に打ちかけを置いてもらうため)
  ・閉じる前の受け皿 … 自分で「閉じる前」(`alltools:before-close`)を受けないツールの画面で、
                  打ちかけ(打った欄・開いている入力の窓)があれば閉じてよいかを訊く
  ・Ctrl+P      … このツールの画面を印刷する(ツールが自分で受けていればそちら)
  ・Alt+1〜9    … 大きなタブを切り替える
  ・F1          … このツールの操作説明書を開く
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
    if (!d) return;
    if (d.type === "alltools:drag") {
      onDrag(d);
      return;
    }
    if (d.type === "alltools:before-close") {
      beforeClose(d);
      return;
    }
    if (d.type === "alltools:python-lost") {
      // このツールの Python が止まった(大きなタブの画面が知らせを重ねている)。
      // 画面は読み直さない ── ツールが自分で受けたければ `alltools:python-lost` を見る
      window.__ALLTOOLS__.pythonLost = true;
      return;
    }
    if ((d.type !== "alltools:result" && d.type !== "alltools:dropped-file") || !pending[d.id]) return;
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
  window.__ALLTOOLS__ = { tool: TOOL, shell: SHELL, embedded: embedded, pythonLost: false,
                         reload: function () { askReload(); } };

  // ---- 閉じる前の受け皿(自分で受けないツールの画面) ----
  //
  //     打った行が消えることがありました
  //
  // 窓の × ・「終了」・F5 の前に、大きなタブの画面が「打ちかけを置いて」と頼んでくる
  // (`alltools:before-close`)。受けて置くのは各ツールの仕事(日報・看板・カレンダーは
  // 自分で受け、`window.__alltoolsHandlesClose = true` を立てる)。**受けないツールの画面は、
  // 返事が無いまま閉じられ、打ちかけが黙って消えていた。** ここで受け皿になる:
  // 打った欄(本人が打って、ページを開いたときの値と違う)や、入力の窓が開いていれば、
  // この画面の中で「閉じますか?」と訊いて答えを返す。何も無ければ訊かずに「済んだ」。
  //
  // 「本人が打った」を見るのは、ツールが自分で値を入れた欄(保存してある値を出した欄)まで
  // 打ちかけに数えて、閉じるたびに訊かないため。保存したかどうかはツールにしか分からない
  // ので、打って保存した欄でも訊くことがある(訊きすぎるほうに倒す)。
  // ブラウザ版はこの台本が入らないので受け皿は無い(自分で受けるツールだけが置く)。
  var touched = [];
  var discardedAt = 0;      // 「閉じてよい」と答えた時刻。そのあと打っていなければ、続けて2度訊かない
  function remember(event) {
    var el = event.target;
    if (!event.isTrusted || !el || !el.tagName) return;
    if (!/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) && !el.isContentEditable) return;
    discardedAt = 0;
    if (touched.indexOf(el) < 0) touched.push(el);
  }
  window.addEventListener("input", remember, true);
  window.addEventListener("change", remember, true);

  var SKIP_TYPES = /^(hidden|button|submit|reset|image|file|search|range|color)$/i;
  function visible(el) {
    if (!el.isConnected) return false;
    if (el.getClientRects && !el.getClientRects().length) return false;
    var style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    return !style || style.visibility !== "hidden";
  }
  function changed(el) {
    if (el.disabled || el.readOnly) return false;
    if (el.isContentEditable && !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return true;
    if (el.tagName === "SELECT") {
      for (var i = 0; i < el.options.length; i++) {
        if (el.options[i].selected !== el.options[i].defaultSelected) return true;
      }
      return false;
    }
    if (el.tagName === "INPUT" && SKIP_TYPES.test(el.type || "")) return false;
    if (el.tagName === "INPUT" && /^(checkbox|radio)$/i.test(el.type || "")) return el.checked !== el.defaultChecked;
    return String(el.value || "") !== String(el.defaultValue || "");
  }
  function openForm() {
    // 開いている入力の窓(`<dialog open>`・aria-modal・よくある .modal の開いた形)で、欄があるもの
    var list = document.querySelectorAll('dialog[open], [aria-modal="true"], .modal.show, .modal.is-open, .modal.open, .modal[open]');
    for (var i = 0; i < list.length; i++) {
      if (visible(list[i]) && list[i].querySelector("input:not([type=hidden]), textarea, select")) return true;
    }
    return false;
  }
  function dirtyHere() {
    touched = touched.filter(function (el) { return el.isConnected; });
    for (var i = 0; i < touched.length; i++) {
      if (visible(touched[i]) && changed(touched[i])) return true;
    }
    return openForm();
  }
  function beforeClose(d) {
    // 自分で受けるツール(`__alltoolsHandlesClose`)には任せる。2重に訊かない
    if (window.__alltoolsHandlesClose) return;
    var reply = function (type, extra) {
      var msg = { type: type, seq: d.seq };
      if (extra) for (var k in extra) msg[k] = extra[k];
      try { parentWin.postMessage(msg, SHELL); } catch (e) { /* 大きなタブが居ない */ }
    };
    reply("alltools:before-close-ack");
    var ok = true;
    try {
      // 外枠は途中の処理の確認のあとにもう一度頼んでくる。答えたあと打っていなければ2度訊かない
      var answered = discardedAt && Date.now() - discardedAt < 120000;
      if (!answered && dirtyHere()) {
        ok = window.confirm("保存していない入力があります。閉じますか?\n\n"
          + "(閉じると、この画面で打った値は消えます。残すときは「キャンセル」を押して保存してください)");
        if (ok) discardedAt = Date.now();
      }
    } catch (e) { ok = true; }
    reply("alltools:before-close-done", { ok: ok });
  }

  // ---- ファイルを送る(FormData)は、中身を読んでから1つのバイト列にして送る ----
  // WebView2(Windows)は、外枠の宛先(各ツールの scheme)へ送る FormData のうち、
  // **ディスクのファイルを指す部分(File)を外枠へ渡さない**。Python には中身の欠けた
  // ファイルが届き、看板の「Access の最新で表の中身を入れ替える」は「ファイルが
  // 小さすぎます」、日報の取り込みも読めなかった(ブラウザ版では起きない)。
  // ここで multipart の本文を自分で組み立てる(中身はメモリの上のバイト列)
  function hasFile(form) {
    var found = false;
    form.forEach(function (value) { if (typeof value !== "string") found = true; });
    return found;
  }
  function quoteName(text) {
    return String(text).replace(/"/g, "%22").replace(/\r/g, "%0D").replace(/\n/g, "%0A");
  }
  function formToBytes(form) {
    var boundary = "----AllToolsForm" + Date.now().toString(16) + Math.random().toString(16).slice(2);
    var enc = new TextEncoder();
    var parts = [];
    var reads = [];
    form.forEach(function (value, name) {
      if (typeof value === "string") {
        parts.push(enc.encode("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" +
          quoteName(name) + "\"\r\n\r\n" + value + "\r\n"));
        return;
      }
      var head = enc.encode("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" +
        quoteName(name) + "\"; filename=\"" + quoteName(value.name || "blob") + "\"\r\nContent-Type: " +
        (value.type || "application/octet-stream") + "\r\n\r\n");
      var at = parts.length;
      parts.push(head, null, enc.encode("\r\n"));
      reads.push(value.arrayBuffer().then(function (buf) { parts[at + 1] = new Uint8Array(buf); }));
    });
    return Promise.all(reads).then(function () {
      parts.push(enc.encode("--" + boundary + "--\r\n"));
      var size = 0;
      parts.forEach(function (p) { size += p.length; });
      var bytes = new Uint8Array(size);
      var offset = 0;
      parts.forEach(function (p) { bytes.set(p, offset); offset += p.length; });
      return { bytes: bytes, type: "multipart/form-data; boundary=" + boundary };
    });
  }
  if (typeof window.fetch === "function" && typeof FormData === "function") {
    var nativeFetch = window.fetch;
    window.fetch = function (input, init) {
      var self = this;
      if (!init || !(init.body instanceof FormData) || !hasFile(init.body)) {
        return nativeFetch.call(self, input, init);
      }
      return formToBytes(init.body).then(function (made) {
        var headers = new Headers(init.headers || {});
        headers.set("Content-Type", made.type);
        var next = {};
        for (var key in init) next[key] = init[key];
        next.body = made.bytes;
        next.headers = headers;
        return nativeFetch.call(self, input, next);
      });
    };
  }

  function tellShell(data, type) {
    if (!embedded || sameOriginParent) return;
    data.type = type || "alltools:key";
    try { parentWin.postMessage(data, SHELL); } catch (e) { /* 大きなタブが居ない */ }
  }

  // ---- ファイルのドラッグ&ドロップ(デスクトップ版。外枠が OS から受けて、ここへ渡す) ----
  // WebView2 に任せると、大きなタブの枠(iframe)の中のこの画面へ落としても届かなかった
  // (現場の Windows で「効かない」)。窓への落下は外枠が受け、大きなタブの画面から
  // **位置と名前・大きさだけ**が届く(src-tauri/src/drops.rs → shell.js)。ここでその位置の
  // 要素に dragenter / dragover / drop を起こす ── ツールの画面は今までどおり `drop` の
  // `dataTransfer.files` で受ける(ツールは書き換えない)。中身は、dragover を受けた要素
  // (落とす枠)の上で離したときだけ取り寄せる
  var dragAt = null;

  function transferOf(files) {
    var dt = new DataTransfer();
    for (var i = 0; i < files.length; i++) dt.items.add(files[i]);
    return dt;
  }
  function placeholders(list) {
    // 上を通るあいだは中身の無い札(名前だけ)。本物のドラッグも、離すまで中身は読めない
    return (list || []).map(function (f) {
      return new File([], String(f.name || "file"), { lastModified: Number(f.modified) || Date.now() });
    });
  }
  function fire(type, target, x, y, dt) {
    var ev = new DragEvent(type, { bubbles: true, cancelable: true, composed: true,
                                   clientX: x, clientY: y, dataTransfer: dt });
    target.dispatchEvent(ev);
    return ev;
  }
  function askDropped(dropSeq, index) {
    return new Promise(function (resolve, reject) {
      seq += 1;
      var id = seq;
      pending[id] = { resolve: resolve, reject: reject };
      try {
        parentWin.postMessage({ type: "alltools:dropped-file", id: id, seq: dropSeq, index: index }, SHELL);
      } catch (e) {
        delete pending[id];
        reject(e);
      }
    });
  }
  function bytesOf(value) {
    if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) return value;
    if (Array.isArray(value)) return new Uint8Array(value);
    throw new Error("ファイルの中身を受け取れませんでした");
  }
  function onDrag(d) {
    if (d.kind === "leave") {
      if (dragAt) fire("dragleave", dragAt, 0, 0, transferOf([]));
      dragAt = null;
      return;
    }
    var x = Number(d.x) || 0;
    var y = Number(d.y) || 0;
    var list = d.files || [];
    var target = document.elementFromPoint(x, y) || document.body || document.documentElement;
    var dt = transferOf(placeholders(list));
    if (dragAt && dragAt !== target) fire("dragleave", dragAt, x, y, dt);
    if (dragAt !== target) fire("dragenter", target, x, y, dt);
    dragAt = target;
    var over = fire("dragover", target, x, y, dt);
    if (d.kind !== "drop") return;
    dragAt = null;
    if (!list.length || !over.defaultPrevented) {
      // 落とす枠の外で離した・フォルダだけ落とした
      fire("dragleave", target, x, y, dt);
      tellShell({ reason: list.length ? "target" : "folder", folders: Number(d.folders) || 0 },
                "alltools:drop-refused");
      return;
    }
    Promise.all(list.map(function (f, i) {
      return askDropped(d.seq, i).then(function (value) {
        return new File([bytesOf(value)], String(f.name || "file"),
                        { lastModified: Number(f.modified) || Date.now() });
      });
    })).then(function (files) {
      fire("drop", target, x, y, transferOf(files));
    }, function (err) {
      fire("dragleave", target, x, y, dt);
      tellShell({ reason: "read", message: String((err && err.message) || err) }, "alltools:drop-refused");
    });
  }

  // ---- 画面が出た(大きなタブの画面が記録に残す。外枠の「起動しています」とは別) ----
  // 確認ダイアログ(alert / confirm / prompt)がブラウザのもの(答えを待つ)かも添える。
  // 置き換えられていると `if (!confirm(..))` が訊かずに進む(起動確認が見る)
  function dialogsKind() {
    try {
      var names = ["alert", "confirm", "prompt"];
      for (var i = 0; i < names.length; i++) {
        var f = window[names[i]];
        if (typeof f !== "function" || !/\[native code\]/.test(Function.prototype.toString.call(f))) {
          return "replaced";
        }
      }
      return "native";
    } catch (e) { return "unknown"; }
  }
  function ready() {
    tellShell({ title: String(document.title || ""), path: location.pathname, dialogs: dialogsKind() },
              "alltools:ready");
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ready);
  } else {
    ready();
  }

  // ---- 読み直し(F5 / Ctrl+R) ----
  // 大きなタブの中では、**大きなタブの画面に頼む**(先に打ちかけを置いてもらってから、
  // この画面だけ読み直す)。以前はここで黙って読み直し、打ちかけが消えていた。
  // ツールの画面の中の枠(同じ宛先)からは、外側の画面の口に頼む。単体(大きなタブの外)なら今までどおり
  function askReload() {
    if (embedded && sameOriginParent) {
      try { if (parentWin.__ALLTOOLS__ && parentWin.__ALLTOOLS__.reload) { parentWin.__ALLTOOLS__.reload(); return; } }
      catch (e) { /* 外側の画面に届かない */ }
      location.reload();
      return;
    }
    if (embedded) {
      tellShell({ action: "reload" });
      return;
    }
    location.reload();
  }

  // ---- キー(このツールの画面にいるとき) ----
  window.addEventListener("keydown", function (event) {
    if (event.defaultPrevented) return;
    var key = event.key || "";
    var ctrl = event.ctrlKey || event.metaKey;
    if (key === "F5" || (ctrl && (key === "r" || key === "R"))) {
      event.preventDefault();
      askReload();
      return;
    }
    if (ctrl && !event.shiftKey && !event.altKey && (key === "p" || key === "P")) {
      // ツールが自分で受けていない Ctrl+P。窓ごとではなく、この画面を刷る
      event.preventDefault();
      window.print();
      return;
    }
    if (key === "F1") {
      // 操作説明書(大きなタブの画面が、このツールの説明書を重ねて開く)
      event.preventDefault();
      tellShell({ action: "help" });
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
