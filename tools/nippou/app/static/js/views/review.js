/*
  views/review.js — 直の終わりの実績確認 (VBA `graphF` を最大化していたもの)

  レールも帯も覆うので、押すまで他の画面へ行けない ── 入力しっ放しで
  直が終わると、間違いに気づくのは翌日の集計になる。

  **ここが直の締めくくり。** 以前は出口が「確認しました」だけで、
  共有へ渡すところへ繋がっていなかった(v3.61.0 で直した)。次に押すもの
  1つ ── 直しに戻る / 共有へ保存して終わる / 確認しました ── を決める
  のはサーバ(`logic/shift_closing.py`)で、ここは写して道を通すだけ。

  「確認した」を覚えるのもサーバ(`logic/shift_review.ReviewGate`)。
  ここで覚えると、タブを開き直すたびにまた出る。
*/

import { api } from "../api.js";
import { toast, toastError } from "../toast.js";
import { paintCharts } from "../chart.js";
import { confirmPush, watch as watchJob } from "../progress.js";
import { cue } from "../sound.js";

export function start() {
  // 「本日の梱包結果」。鳴らすかはサーバが決めて画面に置いてある(直に1回)
  cue(document.getElementById("review")?.dataset.soundCue);

  // サーバが埋め込んだ推移
  const seed = document.getElementById("review-data");
  if (seed) {
    try {
      // **枠の大きさで描く** ── 幅いっぱいに伸ばすと、1枚で画面の半分を超える
      paintCharts(document.getElementById("review-charts"),
                  JSON.parse(seed.textContent), { fit: true });
    } catch { /* 絵が出なくても、表は出ている */ }
  }

  /*
    本当の全画面にする。

    **押した操作が起点なので通る。** ブラウザは操作なしの
    `requestFullscreen()` を断るので、既定の覆い(CSS)と両方用意している。
  */
  document.getElementById("review-fullscreen")?.addEventListener("click", async () => {
    const el = document.documentElement;
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await el.requestFullscreen();
      }
    } catch (err) {
      toast("この端末では全画面にできません(表示はそのままご覧いただけます)", "warn");
    }
  });

  /* ================================================================
     締めくくり ── **この画面で終わらせる**

     VBA は保存すると `graphF` が最大化で出て、それが「この直は済んだ」の
     合図になっていました。こちらは画面こそ出ていたのに、出口が
     「確認しました」だけで**共有へ渡すところへ繋がっていません**でした。

     次に押すもの1つを決めるのはサーバ(`logic/shift_closing.py`)。
     ここは写して、押されたら道を通すだけです。
     ================================================================ */
  let closing = readClosing();
  let stage = "warn";

  /* なぜ出ているのか(と、どの機会で出ているのか)をサーバに訊く */
  (async () => {
    try {
      const body = await api.get("/api/graph/review");
      if (body.stage) stage = body.stage;
      const box = document.getElementById("review-reason");
      if (box && body.reason) {
        box.textContent = body.reason;
        box.hidden = false;
      }
    } catch { /* 理由が出なくても、確認そのものはできる */ }
  })();

  document.getElementById("review-later")?.addEventListener("click",
    () => leave());

  document.getElementById("review-act")?.addEventListener("click", async () => {
    const act = closing.action || "done";
    if (act === "push") { await pushAndPaint(); return; }
    /*
      直しに戻る / 確認しました ── どちらも**この回は答えた**ことにします。

      【覚えさせないと、直しに行けません】
      はじめは「直しに戻る」だけ覚えさせない作りにしていました ──
      直したらまた見てもらうつもりで。ところが戻った先で1分タイマーが
      「まだ出す用がある」と見て、**そのまま全画面へ連れ戻します**:

          直しに戻るを押す → 一瞬だけ入力画面 → すぐ実績画面 → 何もできない

      直すところがあるのは**直してからでないと消えない**ので、覚えない
      かぎり必ずこうなります。押した人は「分かった、直す」と答えたのだ
      から、その回はそこで閉じます。

      直している最中に何が残っているかは、入力画面の帯が出したままに
      しています(`logic/entry_findings.py`)。直し終えたら、その帯が
      緑になって「このまま共有へ保存できます」と言います。
    */
    await leave();
  });

  /** 共有へ渡して、**その場で画面を「渡しました」に変える。** */
  async function pushAndPaint() {
    if (!(await confirmPush())) return;
    const btn = document.getElementById("review-act");
    if (btn) btn.disabled = true;
    // **進み具合を出す**(確かめる → 送る → 写す → 月替わり)。`progress.js`
    const stop = watchJob("共有へ保存しています");
    try {
      const body = await api.post("/api/settings/push", {});
      toast(body.message || "共有へ保存しました", body.failed ? "warn" : "ok");
      await refreshClosing(true);
    } catch (err) {
      // 断られた ── **何が残っているかを、この画面に出す。**
      // 直しに行く道はボタンのほうで開きます
      toastError(err);
      await refreshClosing(false);
    } finally {
      stop();
      if (btn) btn.disabled = false;
    }
  }

  /** サーバに成り行きを訊き直して描く。 */
  async function refreshClosing(pushed) {
    try {
      closing = await api.get(
        `/api/graph/review/closing${pushed ? "?pushed=1" : ""}`);
      paintClosing(closing);
    } catch { /* 描き直せなくても、押した結果はトーストに出ている */ }
  }

  /**
   * 確認画面を閉じる。**この回は答えたことにして、もう出しません。**
   *
   * どの直について出していたかはサーバが持っています(`_review_state`)
   * ── 画面から渡すと、覚える相手がずれて出続けます。
   */
  async function leave() {
    try {
      const body = await api.post("/api/graph/review/ack", { stage });
      toast(body.message, "ok");
      if (document.fullscreenElement) {
        try { await document.exitFullscreen(); } catch { /* 抜けられなくてよい */ }
      }
      location.href = "/";
    } catch (err) { toastError(err); }
  }
}

/** サーバが描いておいた成り行きを読む。読めなければ空。 */
function readClosing() {
  const seed = document.getElementById("closing-data");
  if (!seed) return {};
  try { return JSON.parse(seed.textContent); } catch { return {}; }
}

/**
 * 成り行きを画面へ写す。**判断はしない。**
 *
 * 文言(`headline` / `note` / `label`)も強さ(`level`)もサーバが決めた
 * ものをそのまま出します ── ここで書き分けると、同じ状態に2通りの
 * 言い方ができます。
 */
function paintClosing(closing) {
  if (!closing) return;
  const box = document.getElementById("closing");
  if (box) box.dataset.level = closing.level || "ok";
  const head = document.getElementById("closing-headline");
  if (head) head.textContent = closing.headline || "";
  const note = document.getElementById("closing-note");
  if (note) note.textContent = closing.note || "";

  const list = document.getElementById("closing-list");
  if (list) {
    list.replaceChildren();
    for (const f of closing.findings || []) {
      const li = document.createElement("li");
      const where = document.createElement("b");
      where.textContent = f.where || "";
      li.append(where, ` ${f.message || ""}`);
      list.append(li);
    }
    list.hidden = !(closing.findings || []).length;
  }

  const act = document.getElementById("review-act");
  if (act) {
    act.dataset.action = closing.action || "done";
    act.textContent = closing.label || "確認しました";
  }
  const later = document.getElementById("review-later");
  if (later) later.hidden = !closing.can_push;
}
