/*
  remote_stop.js — 管理者以外停止モード(`portal/remote_stop.py`・`portal/stop_mode.py`)

  1. 帯: 管理者が更新のため停止を求めているあいだ、**全ツールの画面の上に**帯を出し、止まるまでの残りを数える。
     中のツールの「残っているもの」(保存していない入力・実行中の処理)を並べる。止めると決まったら、
     各ツールの画面に打ちかけを置いてもらい(閉じる前の頼みと同じ)、入口へ「置きました」を返す
  2. 大設定の欄: お願いを出す・各 PC の様子を見る・解除する。判断(パスワード・Administrator・出ているか)は
     入口が持つ。ここは並べるだけ
*/
import { api } from "./api.js";
import { toast, toastError } from "./toast.js";

const BAND_POLL_MS = 3000;
const FLUSH_LIMIT_MS = 8000;      // 打ちかけを置いてもらうのを待つ上限(入口は 10 秒で閉じる)
const CARD_POLL_MS = 5000;        // Administrator の端末(各 PC の様子を見る)
const CARD_SLOW_MS = 30000;       // それ以外(お願いが出ているかだけ)

const $ = (id) => document.getElementById(id);
let ctx = { flush: async () => true, ended: () => {}, quit: () => {}, unsaved: () => [] };
let band = null;            // いま帯に出しているもの
let offset = 0;             // 入口の時計との差(ミリ秒)
let tick = null;
let flushedFor = "";        // 打ちかけを置いたお願い
let seenStopping = false;
let misses = 0;            // 続けて届かなかった回数(1回の取りこぼしで「止まった」としない)

function rest(ms) {
  const left = Math.max(0, ms);
  const min = Math.floor(left / 60000);
  const sec = Math.floor((left % 60000) / 1000);
  return `${min}:${String(sec).padStart(2, "0")}`;
}

function bandLine() {
  if (!band) return "";
  if (band.stopping) {
    return `${band.text}。画面の打ちかけを置いてから、日報複合ツールを閉じます。`;
  }
  if ((band.writing || []).length) {
    return `${band.text}。共有へ書いている最中なので、終わるのを待っています(${band.writing.join("、")})。`
      + `書き終わったら止めます(長くても あと ${rest(band.writing_until_ms - (Date.now() + offset))} で強制で止めます)。`;
  }
  const items = (band.left || []).filter(Boolean);
  const what = items.length
    ? `残っているもの: ${items.join("、")}。保存する・処理が終わると自動で止まります。`
    : "保存していない入力・実行中の処理はありません。まもなく止まります。";
  const left = rest(band.deadline_ms - (Date.now() + offset));
  return `${band.text}。${what} あと ${left} で強制で止めます`
    + "(止める前に打ちかけを手元に置き、残っていたものは次の起動で知らせます)。";
}

function paintBand(state) {
  const box = $("stopband");
  if (!box) return;
  band = state && state.id ? state : null;
  if (!band) {
    box.hidden = true;
    if (tick) { clearInterval(tick); tick = null; }
    return;
  }
  if (typeof band.now_ms === "number") offset = band.now_ms - Date.now();
  const paint = () => { $("stopband-line").textContent = bandLine(); };
  paint();
  box.hidden = false;
  if (!tick) tick = setInterval(paint, 1000);
}

async function flush(id) {
  if (flushedFor === id) return;
  flushedFor = id;
  let ok = true;
  try {
    ok = await Promise.race([ctx.flush(), new Promise((resolve) => setTimeout(() => resolve(false), FLUSH_LIMIT_MS))]);
  } catch (err) { ok = false; }
  api.post("/api/remote-stop/flushed", { ok: Boolean(ok) }).catch(() => {});
}

const PAGE = (crypto.randomUUID && crypto.randomUUID()) || `p${Date.now()}${Math.random().toString(36).slice(2)}`;

async function pollBand() {
  let body;
  // この画面(大設定)で保存していない入力を言い添える(残っているものに数える)。**お願いが来る前から**
  // (裏に回した窓はタイマーが間引かれ、お願いを受けてからでは帯を見せる間に間に合わない)
  let query = "";
  if (!(band && band.stopping)) {
    let items = [];
    try { items = ctx.unsaved() || []; } catch (err) { items = []; }
    query = `?page=${encodeURIComponent(PAGE)}&unsaved=${encodeURIComponent(JSON.stringify(items))}`;
  }
  try {
    body = await api.get(`/api/remote-stop/screen${query}`);
    misses = 0;
  } catch (err) {
    // お願いを受けているあいだに続けて届かなくなった = 止まった(ブラウザ版)。「接続できません」ではなく理由を出す
    misses += 1;
    if ((seenStopping || band) && misses >= 2) {
      ctx.ended("管理者が更新のため日報複合ツールを止めました(管理者以外停止モード)。"
        + "更新が終わるまで起動できません。しばらくしてから Start.vbs で開き直してください。");
      seenStopping = false;
      paintBand(null);
    }
    return;
  }
  for (const note of body.notes || []) toast(note, "warn", 0);
  const state = body.state;
  paintBand(state);
  if (state && state.stopping) {
    seenStopping = true;
    flush(state.id);
  } else {
    seenStopping = false;
  }
}

export function startBand(options) {
  ctx = { ...ctx, ...options };
  const quit = $("stopband-quit");
  if (quit) quit.addEventListener("click", () => ctx.quit());
  pollBand();
  return setInterval(pollBand, BAND_POLL_MS);
}

// ------------------------------------------------------------------
// 大設定の欄
// ------------------------------------------------------------------
let cardTimer = null;
let admin = false;

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text || "";
  return td;
}

function paintCard(view) {
  if (!$("rs-state")) return;
  const active = Boolean(view.request);
  const state = $("rs-state");
  state.textContent = !view.available ? "使えません" : active ? "お願いを出しています(更新中)" : "出していません";
  state.classList.toggle("is-warn", !view.available || active);
  const why = [];
  if (!view.available) why.push(view.why);
  if (view.unreadable) why.push(view.unreadable);
  if (view.available && !view.administrator) {
    why.push(`${view.administrator_why || "この端末に Administrator がありません"}。お願いは出せません。`);
  }
  $("rs-why").textContent = why.filter(Boolean).join(" ");
  $("rs-why").hidden = !why.length;
  $("rs-request").textContent = active ? view.request_text : "";
  $("rs-request").hidden = !active;
  if (view.folder) $("rs-folder").textContent = view.folder;
  $("rs-start").hidden = active;
  $("rs-clear").hidden = !active;
  $("rs-start").disabled = !view.available || !view.administrator;
  $("rs-clear").disabled = !view.available || !view.administrator;
  const body = $("rs-pcs").querySelector("tbody");
  body.replaceChildren();
  for (const pc of view.pcs || []) {
    const tr = document.createElement("tr");
    if (pc.stale) tr.className = "is-stale";
    tr.append(cell(pc.pc), cell(pc.login), cell(pc.app), cell(pc.version), cell(pc.label), cell(pc.detail), cell(pc.beat));
    body.append(tr);
  }
  $("rs-pcs-wrap").hidden = !(view.pcs || []).length;
}

function cardShown() {
  // 大設定のタブを開いていて、タブが表にあるときだけ見に行く(全 PC が数秒ごとに共有を読まない)
  const panel = $("rs-card") && $("rs-card").closest(".panel");
  return Boolean(panel && !panel.hidden && panel.classList.contains("is-shown")
    && document.visibilityState === "visible");
}

async function readCard() {
  const view = await api.get("/api/remote-stop");
  admin = Boolean(view.administrator);
  paintCard(view);
}

function scheduleCard() {
  if (cardTimer) clearTimeout(cardTimer);
  cardTimer = setTimeout(async () => {
    if (cardShown()) {
      try { await readCard(); } catch (err) { /* 黙って次を待つ(押したときの断りはトーストで出す) */ }
    }
    scheduleCard();
  }, admin ? CARD_POLL_MS : CARD_SLOW_MS);
}

async function send(path, body) {
  try {
    const res = await api.post(path, body);
    toast(res.message, "ok", 7000);
    if (res.state) paintCard(res.state);
  } catch (err) {
    toastError(err);
    if (err.body && err.body.state) paintCard(err.body.state);
  } finally {
    $("rs-password").value = "";
  }
}

export function attachCard() {
  if (!$("rs-card")) return;
  $("rs-start").addEventListener("click", () => {
    const reason = $("rs-reason").value.trim() || "更新のため";
    if (!confirm("ほかの PC の日報複合ツールと4ツールを止めます(この PC は止めません)。\n\n"
      + "・保存していない入力・実行中の処理が無い PC は、まもなく止まります\n"
      + "・残っている PC には帯で知らせ、利用者が保存・終了するのを待ちます\n"
      + "・5分たっても止まらなければ強制で止めます(打ちかけは手元に置き、残っていたものは次の起動で知らせます)\n"
      + "・解除するまで、ほかの PC では起動できません\n"
      + "・ランチャーの「全 PC で止める」とは同時に使いません\n\n"
      + `理由: ${reason}\nよろしいですか?`)) return;
    send("/api/remote-stop/start", { password: $("rs-password").value, reason });
  });
  $("rs-clear").addEventListener("click", () => {
    if (!confirm("解除します。ほかの PC もまた起動できるようになります。更新は終わりましたか?")) return;
    send("/api/remote-stop/clear", { password: $("rs-password").value });
  });
  // 最初の1回は見えていなくても読む(開いたときに「確かめています…」のまま残さない)
  readCard().catch(() => {});
  scheduleCard();
}

/** 大設定のタブを開いたとき(すぐ読み直す) */
export function cardOpened() {
  readCard().catch(() => {});
}
