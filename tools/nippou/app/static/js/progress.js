/*
  progress.js — 時間のかかる仕事の進み具合を、画面の上に出す

      保存処理にもプログレスを表示し進捗がわかるようにしてください

  共有へ保存・マスタの1行を直す、はどちらも**終わるまで返りません。**
  共有のフォルダが遠い日は何秒もかかり、そのあいだ画面はボタンがくるくる
  回るだけでした ── 何をしていて、あとどれくらいかが分からないと、人は
  もう一度押すか、閉じます。

  **数と言葉を持つのはサーバ**(`nippou/job_progress.py` と
  `logic/progress.py`)。ここは 0.4 秒ごとに `GET /api/progress` を見に行き、
  返ってきた字をそのまま出すだけです。

  枠は画面の上の真ん中に浮かせます(画面ごとに置き場所を作らない ──
  日報入力でも設定でも、マスタの窓の上でも同じ所に出ます)。
  **0.3 秒より早く終わるものには出しません**(ちらつくだけなので)。

      const stop = watch("共有へ保存しています");
      try { await api.post(...); } finally { stop(); }
*/
import { background } from "./api.js";

const POLL_MS = 400;
const SHOW_AFTER_MS = 300;

function panel() {
  let box = document.getElementById("job-float");
  if (box) return box;
  box = document.createElement("div");
  box.id = "job-float";
  box.className = "job job--float";
  box.hidden = true;
  box.setAttribute("role", "status");
  box.setAttribute("aria-live", "polite");
  box.innerHTML = '<div class="job__title"></div>'
    + '<div class="job__head"></div>'
    + '<progress class="job__bar" max="100"></progress>'
    + '<div class="lead job__note"></div>';
  document.body.append(box);
  return box;
}

function paint(box, now, title) {
  box.querySelector(".job__title").textContent = now.title || title || "";
  box.querySelector(".job__head").textContent = now.headline || title || "";
  box.querySelector(".job__note").textContent = now.note || "";
  const bar = box.querySelector(".job__bar");
  // **総数が分からないうちは、止まった棒を出さない**(動いている棒にする)
  if (now.total > 0) bar.value = now.percent;
  else bar.removeAttribute("value");
}

let active = 0;       // 同時に見ている数(入れ子になっても最後の1つで消す)

/**
 * 進み具合を見はじめる。返り値を呼ぶと止めて枠を消す。**必ず呼ぶ**
 * (`finally`)── 呼ばないと枠が出たままになります。
 */
export function watch(title = "") {
  const box = panel();
  active += 1;
  let stopped = false;
  let timer = 0;
  const show = setTimeout(() => {
    if (stopped) return;
    paint(box, { headline: title, total: 0 }, title);
    box.hidden = false;
    timer = setInterval(async () => {
      try {
        // 誰も押していない通信。**待機の姿を出さない**(`background`)
        const now = await background.get("/api/progress");
        if (!stopped && now && now.running) paint(box, now, title);
      } catch { /* 読めなくても仕事は進んでいます */ }
    }, POLL_MS);
  }, SHOW_AFTER_MS);

  return () => {
    if (stopped) return;
    stopped = true;
    clearTimeout(show);
    clearInterval(timer);
    active = Math.max(0, active - 1);
    if (!active) box.hidden = true;
  };
}
