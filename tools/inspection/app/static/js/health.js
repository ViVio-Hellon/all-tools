/*
  health.js — 生存監視と心拍 (基盤仕様書 2.8 / 2.9)

  python-web-tools の health.js と同じ作り。
  ・`/api/health` を定期的に見る。2回続けて届かなければ赤い帯を出す
  ・`/api/alive` に心拍を送る。閉じたら `sendBeacon` で「閉じました」
  ・裏に回った / 固まった / スリープから戻った、をサーバに伝える
    (裏のタブはタイマーが間引かれるので、そう言った画面は落とさせない)
*/

import { api, screenId } from "./api.js";
import * as clientlog from "./clientlog.js";

const MISSES_BEFORE_OFFLINE = 2;

let misses = 0;
let wasOffline = false;
let lastPid = null;
const listeners = [];

/** 生存確認の答えを受け取る(帯の「動いているもの」・再同期)。 */
export function onHealth(fn) { listeners.push(fn); }

function setOffline(offline) {
  const banner = document.getElementById("offline");
  if (banner) banner.hidden = !offline;
}

async function beat() {
  try {
    const body = await api.get("/api/health");
    const reason = wasOffline ? "reconnected" : "";
    if (misses >= MISSES_BEFORE_OFFLINE) setOffline(false);
    misses = 0;
    clientlog.flushOffline();       // 切れていたあいだのことを残す(切れていなければ何もしない)
    wasOffline = false;
    // **別のプロセスに入れ替わっていたら**(再起動・入れ替え)、この画面の
    // トークンはもう通らない。読み込み直して新しい画面を受け取る
    //
    // **打ちかけの部数と、設定に打ちかけたフォルダは持ち越す。** 以前は黙って
    // 読み込み直し、打った部数が1に、設定の欄に打ちかけたパスが消えていた
    // (「打った値が勝手に戻ることがありました」)。選んだ点検表はもともと
    // `sessionStorage` に控えてある
    if (lastPid !== null && body && body.pid !== lastPid) {
      keepTyped();
      location.replace("/");
      return;
    }
    if (body) lastPid = body.pid;
    // 共有フォルダのアプリが新しい版に入れ替えられた。動いているのは古い版のまま
    if (body && body.version_on_disk && body.version_on_disk !== window.APP.version) {
      const box = document.getElementById("updated");
      if (box && box.hidden) {
        document.getElementById("updated-text").textContent =
          `新しい版(VER${body.version_on_disk})が置かれました。いま動いているのは VER${window.APP.version} です。`;
        box.hidden = false;
      }
    }
    for (const fn of listeners) fn(body, reason);
  } catch {
    if (++misses === MISSES_BEFORE_OFFLINE) { setOffline(true); wasOffline = true; }
  }
}

/* ---------------------------------------------------------------- 持ち越し */
const CARRY_KEY = "isp.carry";
// 持ち越しを使う期限。古い控えで、あとから開いた画面の値を上書きしない
const CARRY_MS = 10 * 60 * 1000;

function keepTyped() {
  try {
    const dialog = document.getElementById("settings-dialog");
    const folder = dialog && dialog.open ? document.getElementById("settings-folder").value : null;
    const copies = document.getElementById("copies");
    sessionStorage.setItem(CARRY_KEY, JSON.stringify({
      copies: copies ? copies.value : null, folder, at: Date.now() }));
  } catch { /* 控えられなくても読み込み直しはする */ }
}

/**
 * 読み込み直す前に打っていた値を受け取る(1回だけ)。無ければ null。
 * @returns {{copies: (string|null), folder: (string|null)} | null}
 */
let carried;
export function takeCarried() {
  if (carried !== undefined) return carried;
  carried = null;
  try {
    const raw = sessionStorage.getItem(CARRY_KEY);
    sessionStorage.removeItem(CARRY_KEY);
    const data = raw ? JSON.parse(raw) : null;
    if (data && Date.now() - Number(data.at || 0) < CARRY_MS) carried = data;
  } catch { carried = null; }
  return carried;
}

/** いますぐ確かめる(再接続ボタン・表に戻ったとき)。 */
export function checkNow() { beat(); }

/* ================================================================
   こちらが生きていることを伝える (基盤仕様書 2.8「自動終了」)
   ================================================================ */
function alive(body) {
  const payload = JSON.stringify({ ...(body || {}), screen_id: screenId() });
  // 閉じる・裏に回る瞬間は `sendBeacon`。このあとタブが固まっても送りきる
  if (body && (body.leaving || body.visible === false) && navigator.sendBeacon) {
    navigator.sendBeacon("/api/alive", new Blob([payload], { type: "application/json" }));
    return;
  }
  fetch("/api/alive", {
    method: "POST", cache: "no-store",
    headers: { "Content-Type": "application/json" }, body: payload,
  }).then((res) => res.text())
    .catch(() => { /* 届かなくても画面は続く。次の心拍で取り戻す */ });
}

let lastTick = Date.now();
let lastResume = 0;
let stopped = false;

function isVisible() { return document.visibilityState === "visible"; }

function comeBack(gapMs) {
  if (stopped) return;
  const now = Date.now();
  if (now - lastResume < 1000 && !gapMs) return;
  lastResume = now;
  lastTick = now;
  alive({ resumed: true, visible: isVisible(), gap_ms: gapMs || 0 });
  misses = 0;
  beat();
}

function goAway() { if (!stopped) alive({ visible: false }); }

function startAlive() {
  const period = window.APP.alivePollMs || 20000;
  alive({ visible: isVisible() });
  setInterval(() => {
    if (stopped) return;
    const now = Date.now();
    const gap = now - lastTick;
    lastTick = now;
    // 間隔が大きく空いた = PCが寝ていた(または固まっていた)
    if (gap > Math.max(period * 3, 120000)) { comeBack(gap); return; }
    alive({ visible: isVisible() });
  }, period);

  window.addEventListener("pagehide", (event) => {
    if (stopped) return;
    if (event.persisted) goAway();
    else alive({ leaving: true });
  });
  window.addEventListener("pageshow", (event) => { if (event.persisted) comeBack(0); });
  document.addEventListener("visibilitychange", () => { if (isVisible()) comeBack(0); else goAway(); });
  document.addEventListener("freeze", goAway);
  document.addEventListener("resume", () => comeBack(0));
  window.addEventListener("focus", () => comeBack(0));
  window.addEventListener("online", () => comeBack(0));
}

let healthTimer = null;

export function startHeartbeat() {
  const period = window.APP.healthPollMs || 15000;
  beat();
  healthTimer = setInterval(beat, period);
  startAlive();
  const reconnect = document.getElementById("reconnect");
  if (reconnect) reconnect.addEventListener("click", () => { misses = 0; beat(); });
}

/** 終了したあとは見張らない(赤い帯を出さない)。 */
export function stopHeartbeat() {
  stopped = true;
  clearInterval(healthTimer);
  setOffline(false);
}
