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

  function el(tag, css, text) {
    var node = document.createElement(tag);
    if (css) node.style.cssText = css;
    if (text) node.textContent = text;
    return node;
  }

  function ensureBox() {
    if (box) return;
    box = el("div", "position:fixed;top:0;left:0;right:0;z-index:2147483000;display:none;gap:4px 12px;"
      + "flex-wrap:wrap;align-items:center;padding:10px 16px;background:#fdf0f0;color:#982326;"
      + "border-bottom:3px solid #c62828;box-shadow:0 2px 8px rgba(0,0,0,.25);font:14px/1.5 sans-serif");
    box.id = "remote-stop-band";
    box.setAttribute("role", "alert");
    var title = el("b", "font-size:15px;flex:0 0 auto", "管理者が更新のため停止を求めています");
    line = el("span", "flex:1 1 360px;min-width:0");
    box.appendChild(title);
    box.appendChild(line);
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
    var items = (state.left || []).filter(Boolean);
    var what = items.length
      ? "残っているもの: " + items.join("、") + "。保存する・処理が終わると自動で止まります。"
      : "保存していない入力・実行中の処理はありません。まもなく止まります。";
    return state.text + "。" + what + " あと " + rest(state.deadline_ms - (Date.now() + offset))
      + " で強制で止めます(止める前に打ちかけを手元に置き、残っていたものは次の起動で知らせます)。";
  }

  function paint() {
    if (!box) return;
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
    // 枠の中(日報複合ツール)でも言い添える(入口がまとめて大きなタブの帯に出す)
    if (state && !state.stopping) {
      url += "&unsaved=" + encodeURIComponent(JSON.stringify(unsaved()));
    }
    fetch(url, { cache: "no-store" }).then(function (res) { return res.json(); }).then(function (body) {
      misses = 0;
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
      if ((stopping || state) && misses >= 2 && box && !(state && state.hosted && inFrame)) {
        line.textContent = "管理者が更新のため止めました(管理者以外停止モード)。更新が終わるまで起動できません。"
          + "しばらくしてから開き直してください。";
        box.style.display = "flex";
        stopping = false;
        state = null;
        clearInterval(painter);
      }
    });
  }

  var painter = null;
  function start() {
    poll();
    setInterval(poll, POLL_MS);
    painter = setInterval(paint, 1000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
