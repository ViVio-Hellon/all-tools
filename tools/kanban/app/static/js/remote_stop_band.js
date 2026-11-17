/*
  remote_stop_band.js — 管理者以外停止のお願いの帯(`remote_stop.py`。4ツールで同じ中身)

  日報複合ツールの大設定から、管理者が更新のため停止を求めているあいだ、画面の上に帯を出して
  止まるまでの残りを数える。保存していない入力・実行中の処理が無くなれば自動で止まり、
  残っていても期限で強制で止まる(止める前に打ちかけを置く。残っていたものは次の起動で知らせる)。

  - 問い合わせは `/api/health?remote_stop=1`(起動トークンが要らない。業務のデータは含まない)
  - この画面で保存していない入力(`window.__remoteStopUnsaved` が返す呼び名)を言い添える
    → 「残っているもの」に数えて、利用者が保存するのを待つ
  - 止めると決まったら、打ちかけを置く(`window.__remoteStopFlush`。無ければ打っている欄を離れて
    自動保存を走らせる)
  - 日報複合ツールの中(枠の中)で動いているときは、帯は大きなタブの画面が出す(ここでは出さない)
  - 一度だけ出す一言(前に強制で止めたときの控え・お願いの取り下げ)は、閉じるまで残す
*/
(function () {
  "use strict";
  var POLL_MS = 3000;
  var FLUSH_LIMIT_MS = 8000;
  var PAGE = "p" + Date.now().toString(36) + Math.random().toString(36).slice(2);
  var inFrame = window.parent !== window;
  var state = null;
  var offset = 0;
  var flushed = "";
  var stopping = false;
  var misses = 0;
  var box = null;
  var line = null;
  var notes = null;
  // **アプリに届かない**(1.5.125)。最小化していてお願いを受け取る前に止まったときも、大きく出して入力させない
  var offline = false;
  var title = null;
  var sub = null;

  function el(tag, css, text) {
    var node = document.createElement(tag);
    if (css) node.style.cssText = css;
    if (text) node.textContent = text;
    return node;
  }

  function ensureBox() {
    if (box) return;
    // **画面いっぱいに大きく**(1.5.118 ──「停止の案内が小さくて気が付かない」)。後ろは触らせない
    box = el("div", "position:fixed;inset:0;z-index:2147483000;display:none;align-items:center;"
      + "justify-content:center;padding:24px;background:rgba(0,0,0,.65);font:16px/1.5 sans-serif");
    box.id = "remote-stop-band";
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-modal", "true");
    var panel = el("div", "width:min(920px,100%);max-height:100%;overflow:auto;display:flex;flex-direction:column;"
      + "align-items:center;gap:14px;padding:36px 32px;text-align:center;border-radius:16px;background:#fdf0f0;"
      + "color:#982326;border:8px solid #c62828;box-shadow:0 8px 40px rgba(0,0,0,.5)");
    title = el("b", "font-size:clamp(2.4rem,6vw,4.4rem);line-height:1.15;color:#c62828",
                   "入力をやめてください");
    sub = el("b", "font-size:clamp(1.3rem,2.6vw,2rem)", "管理者が更新のため、止めます");
    line = el("span", "font-size:16px;max-width:60em");
    var again = el("button", "font-size:18px;padding:8px 24px;cursor:pointer;display:none", "開き直す");
    again.type = "button";
    again.id = "remote-stop-reload";
    again.addEventListener("click", function () { location.reload(); });
    panel.appendChild(title);
    panel.appendChild(sub);
    panel.appendChild(line);
    panel.appendChild(again);
    box.appendChild(panel);
    document.body.appendChild(box);
  }

  function ensureNotes() {
    if (notes) return notes;
    notes = el("div", "position:fixed;right:16px;bottom:16px;z-index:2147483000;display:grid;gap:8px;max-width:520px");
    notes.id = "remote-stop-notes";
    document.body.appendChild(notes);
    return notes;
  }

  function showNote(text) {
    var host = ensureNotes();
    var card = el("div", "background:#fff8e1;color:#5d4200;border:1px solid #e0b000;border-left:5px solid #c62828;"
      + "border-radius:6px;padding:10px 12px;font:14px/1.5 sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.2)", text);
    card.className = "remote-stop-note";
    var close = el("button", "margin-left:8px;cursor:pointer", "閉じる");
    close.type = "button";
    close.addEventListener("click", function () { card.remove(); });
    card.appendChild(close);
    host.appendChild(card);
  }

  function rest(ms) {
    var left = Math.max(0, ms);
    var min = Math.floor(left / 60000);
    var sec = Math.floor((left % 60000) / 1000);
    return min + ":" + (sec < 10 ? "0" : "") + sec;
  }

  function text() {
    if (!state) return "";
    if (state.stopping) return state.text + "。打ちかけを置いてから止めます。";
    if ((state.writing || []).length) {
      return state.text + "。共有へ書いている最中なので、終わるのを待っています(" + state.writing.join("、")
        + ")。書き終わったら止めます(長くても あと " + rest(state.writing_until_ms - (Date.now() + offset))
        + " で強制で止めます)。";
    }
    var items = (state.left || []).filter(Boolean);
    var what = items.length
      ? "残っているもの: " + items.join("、") + "。保存する・処理が終わると自動で止まります。"
      : "保存していない入力・実行中の処理はありません。まもなく止まります。";
    return state.text + "。" + what + " あと " + rest(state.deadline_ms - (Date.now() + offset))
      + " で強制で止めます(止める前に打ちかけを手元に置き、残っていたものは次の起動で知らせます)。";
  }

  function showOffline(known) {
    ensureBox();
    offline = true;
    title.textContent = "入力をやめてください";
    sub.textContent = known ? "管理者が更新のため止めました" : "アプリが止まっています";
    line.textContent = known
      ? "管理者以外停止モードで止めました。更新が終わるまで起動できません。しばらくしてから開き直してください。"
      : "いま打っても保存されません(管理者の更新で止めたか、アプリが終わっています)。"
        + "しばらくしてから「開き直す」を押してください。";
    document.getElementById("remote-stop-reload").style.display = "";
    box.style.display = "flex";
  }

  function paint() {
    if (!box) return;
    if (offline) { box.style.display = "flex"; return; }
    if (!state || (state.hosted && inFrame)) { box.style.display = "none"; return; }
    line.textContent = text();
    box.style.display = "flex";
  }

  function unsaved() {
    try {
      var fn = window.__remoteStopUnsaved;
      var items = typeof fn === "function" ? fn() : [];
      return Array.isArray(items) ? items.map(String).filter(Boolean) : [];
    } catch (err) { return []; }
  }

  function flush(id) {
    if (flushed === id) return;
    flushed = id;
    try {
      var fn = window.__remoteStopFlush;
      if (typeof fn === "function") {
        Promise.race([Promise.resolve().then(fn), new Promise(function (r) { setTimeout(r, FLUSH_LIMIT_MS); })])
          .catch(function () {});
        return;
      }
      var active = document.activeElement;
      if (active && active !== document.body && typeof active.blur === "function") active.blur();
    } catch (err) { /* 置けなくても止まる */ }
  }

  function poll() {
    var url = "/api/health?remote_stop=1&page=" + encodeURIComponent(PAGE);
    // 枠の中(日報複合ツール)でも言い添える(入口がまとめて大きなタブの帯に出す)。
    // **お願いが来る前から言い添える**: 裏に回したタブはタイマーが間引かれ(長く裏だと1分に1回)、
    // お願いを受けてから言い添えるのでは、帯を見せる間に間に合わず「何も残っていない」で止まっていた
    if (!(state && state.stopping)) {
      url += "&unsaved=" + encodeURIComponent(JSON.stringify(unsaved()));
    }
    fetch(url, { cache: "no-store" }).then(function (res) { return res.json(); }).then(function (body) {
      misses = 0;
      if (offline) {
        // また届いた(アプリが起き直した)。前の画面のままでは打てないので、開き直してもらう
        line.textContent = "アプリはまた動いています。「開き直す」を押してから入力してください。";
        return;
      }
      var rs = body && body.remote_stop;
      ((rs && rs.notes) || []).forEach(showNote);
      state = rs && rs.state && rs.state.id ? rs.state : null;
      if (state && typeof state.now_ms === "number") offset = state.now_ms - Date.now();
      ensureBox();
      paint();
      if (state && state.stopping) {
        stopping = true;
        if (!state.hosted) flush(state.id);
      } else {
        stopping = false;
      }
    }).catch(function () {
      // お願いを受けているあいだに続けて届かなくなった = 止まった(止めると決まってから止まるまでは短く、
      // 問い合わせの間に収まることが多い)。「接続できません」ではなく理由を出す。1回の取りこぼしでは出さない
      misses += 1;
      if (misses >= 2 && !offline && !(state && state.hosted && inFrame)) {
        // お願いを見ていなくても(最小化していて問い合わせが間引かれた)、届かなければ大きく出して止める
        showOffline(!!(stopping || state));
        stopping = false;
        state = null;
      }
    });
  }

  // 閉じた画面の打ちかけを、いつまでも「残っているもの」に数えない
  window.addEventListener("pagehide", function () {
    try {
      fetch("/api/health?remote_stop=1&page=" + encodeURIComponent(PAGE) + "&unsaved=%5B%5D",
        { cache: "no-store", keepalive: true }).catch(function () {});
    } catch (err) { /* 届かなくても、しばらくで忘れる */ }
  });

  function start() {
    poll();
    setInterval(poll, POLL_MS);
    setInterval(paint, 1000);
    // 最小化・裏から戻ったら**すぐ**問い合わせる(裏ではタイマーが1分に1回まで間引かれる)
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") poll();
    });
    window.addEventListener("focus", poll);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
