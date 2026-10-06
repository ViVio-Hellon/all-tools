/*
  app.js — 外枠の配線

  どの画面でも同じもの:
    ・接続の見張りと心拍            (health.js)
    ・同期の状態を帯とレールに出す  (sync.js)
    ・「今すぐ同期」と「終了」       (ここ)
    ・押したボタンの待機表示         (busy.js)

  画面ごとの中身は `views/*.js` が持つ。
*/

import { ApiError, api } from "./api.js";
import * as busy from "./busy.js";
import { isDesktop } from "./desktop.js";
import * as health from "./health.js";
import { confirm, inform } from "./modal.js";
import * as screen from "./screen.js";
import * as sync from "./sync.js";
import * as theme from "./theme.js";
import { toast, toastError } from "./toast.js";

busy.watchClicks();
// 帯の「暗くする / 明るくする」(`theme.js`)
theme.wireToggle();
screen.wireTakeOver();
// **この端末で使う画面は1つだけ。** 2枚目は名乗った時点で断られ、
// 操作できない面が被さる(`screen.js`)
screen.claim();
health.watch();
health.beat();
sync.wireRibbon();
sync.start();
// **表に戻った・スリープから戻ったら、同期の状態を取り直す。** 裏にいた
// あいだは間引かれて古いままなので、次の定期問い合わせを待たせない。
// 取り込み直しがあれば、帯の購読者(カレンダー)が描き直す
health.onResume(() => sync.refresh());

// 画面で起きたエラーは `errlog.js` がサーバへ送り、記録番号が返ってくる。
// **番号を見せる。** 黙っていると「押しても何も起きない」としか伝わらず、
// 後からどの記録かを探せない
document.addEventListener("app:client-error", (event) => {
  toast(`画面でエラーが起きました(記録番号 ${event.detail.ref})。`
        + "うまく動かないときは、この番号を管理者に伝えてください。", "warn");
});

/* ------------------------------------------------------------------
   今すぐ同期
   ------------------------------------------------------------------ */
const syncNow = document.getElementById("sync-now");
if (syncNow) {
  syncNow.addEventListener("click", async () => {
    try {
      const result = await api.post("/api/sync/now");
      sync.apply(result.sync);
      toast(result.message, result.sync && result.sync.offline ? "warn" : "ok");
      // 取り込みで内容が変わっているので、画面に描き直させる
      document.dispatchEvent(new CustomEvent("app:synced"));
    } catch (err) {
      if (err instanceof ApiError && err.code === "not_configured") {
        await inform("同期の設定が要ります",
                     `${err.message}\n\n設定画面で参照パス(取り込み元のフォルダ)を指定してください。`);
        return;
      }
      toastError(err);
    }
  });
}

/* ------------------------------------------------------------------
   終了 (基盤仕様書 2.8)

   窓の無いアプリなので、**終わり方を明示的に置く**。タブを閉じただけでも
   心拍が途切れて終わるが(idle_exit)、その場で終わらせたい人が居る。
   ------------------------------------------------------------------ */
const quit = document.getElementById("quit");
if (quit) {
  quit.addEventListener("click", async () => {
    // デスクトップ版は統合ツールの窓の中で動く。終えると、ほかのツールのタブも閉じる
    if (!await confirm("終了しますか?",
                       isDesktop
                         ? "統合ツールを終了します(ほかのツールのタブもまとめて閉じます)。"
                         : "このアプリを終了します。\n"
                           + "続けて使う場合は、もう一度 Start.vbs から起動してください。",
                       { okLabel: "終了する", danger: true })) return;
    await requestShutdown(false);
  });
}

async function requestShutdown(force) {
  try {
    await api.post("/api/shutdown", { force });
    showFarewell();
  } catch (err) {
    // 409 = 同期の途中。**中断してよいかは利用者が決める**
    if (err instanceof ApiError && err.code === "busy") {
      const running = (err.body && err.body.running) || [];
      const ok = await confirm(
        "実行中の処理があります",
        `${running.join(" / ")} が動いています。\n`
        + "中断して終了しますか?(送れていない入力は次回に送られます)",
        { okLabel: "中断して終了", danger: true });
      if (ok) await requestShutdown(true);
      return;
    }
    toastError(err);
  }
}

function showFarewell() {
  document.body.innerHTML =
    '<div style="height:100%;display:grid;place-items:center;'
    + 'font-family:var(--sans);color:var(--muted);text-align:center;line-height:2">'
    + (isDesktop
      // デスクトップ版: 未送信を送り終えたら、外枠が窓を閉じる(`bridge.py`)
      ? '<div><p style="font-size:17px;color:var(--ink)">終了しています…</p>'
        + '<p style="font-size:13px">送れていない入力があれば送ってから閉じます。</p></div></div>'
      : '<div><p style="font-size:17px;color:var(--ink)">終了しました</p>'
        + '<p style="font-size:13px">このタブは閉じてかまいません。</p></div></div>');
}
