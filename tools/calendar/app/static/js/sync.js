/*
  sync.js — 取り込み元との同期の状態を、帯とレールに出す

  **見張りは1本だけ。** 帯もレールも設定画面も同じ事実(送信待ちの件数・
  送れているか)を要るが、それぞれが `GET /api/sync` を叩くと
  「同じ事実を二か所で持つ」ことになり、片方だけ古い、が起こる。
  ここが1本だけ持ち、欲しい側が `subscribe()` で受け取る。

  **判断は何もしない。** 返ってきたものを写すだけ。
  文言(「送信待ち 3 件」など)はサーバが組み立てている。

  間隔は状態で変える ── 送れていないとき・送信待ちがあるときは短く、
  落ち着いているときは長く。共有フォルダを無駄に叩かない。
*/

import { api } from "./api.js";
import { inform } from "./modal.js";

const FAST_MS = window.APP.syncPollMs || 1000;
const SLOW_MS = 15000;

const listeners = new Set();
let latest = null;
let timer = null;

export function subscribe(fn) {
  listeners.add(fn);
  if (latest) fn(latest);
  return () => listeners.delete(fn);
}

export function current() {
  return latest;
}

/** いますぐ取りに行く(登録・削除の直後など)。 */
export async function refresh() {
  try {
    apply(await api.get("/api/sync"));
  } catch (_err) {
    // 取れないことは `health.js` の見張りが出す。ここでは黙る
  }
  return latest;
}

/** 別の経路で貰った状態を反映する(月APIが一緒に返してくるもの)。 */
export function apply(status) {
  if (!status) return;
  latest = status;
  listeners.forEach((fn) => fn(status));
  schedule();
}

function schedule() {
  clearTimeout(timer);
  const busy = latest && (latest.busy || latest.pending > 0 || latest.offline);
  timer = setTimeout(refresh, busy ? FAST_MS : SLOW_MS);
}

export function start() {
  refresh();
}

/* ------------------------------------------------------------------
   帯とレールへの写し
   ------------------------------------------------------------------ */
/* ------------------------------------------------------------------
   送らなかった登録の知らせ

   同じ人・同じ日を別の端末が先に登録していると、こちらの登録は
   取り込み元へ送られずに取りやめになる。**黙って消さない** ──
   確かめたと押されるまで出す。文言はサーバが組む(`sync/notices.py`)。

   出すのは**使っている画面だけ**(2枚目のタブで押しても断られる)。
   ------------------------------------------------------------------ */
let showingSkipped = false;

function watchSkipped(status) {
  const skipped = status.skipped;
  if (!skipped || !skipped.count || showingSkipped) return;
  const block = document.getElementById("screen-block");
  if (block && !block.hidden) return;            // 使っていない画面
  if (document.querySelector(".modal")) return;  // 操作の途中は割り込まない
  showingSkipped = true;
  inform(skipped.title, skipped.text)
    .then(() => api.post("/api/sync/skipped/ack"))
    .then((next) => apply(next))
    .catch(() => {})
    .finally(() => { showingSkipped = false; });
}

export function wireRibbon() {
  const slot = document.getElementById("rb-sync-slot");
  const text = document.getElementById("rb-sync");
  const badge = document.querySelector('[data-rail-badge="settings"]');

  subscribe(watchSkipped);
  subscribe((status) => {
    if (text) text.textContent = status.text;
    if (slot) {
      slot.classList.toggle("slot--alert", !!status.offline);
      slot.classList.toggle("slot--ok", !status.offline && status.pending === 0
                                        && status.configured);
    }
    if (badge) {
      if (status.offline) {
        badge.textContent = "!";
        badge.className = "badge badge--alert";
        badge.hidden = false;
      } else if (status.pending) {
        badge.textContent = String(status.pending);
        badge.className = "badge badge--wait";
        badge.hidden = false;
      } else {
        badge.hidden = true;
      }
    }
  });
}
