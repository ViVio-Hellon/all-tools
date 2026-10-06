/*
  app.js — 画面の起動処理(python-web-tools の app.js と同じ役目)

  外枠(心拍・帯・終了)を繋いでから、点検表の画面を動かす。
*/

import { api } from "./api.js";
import * as busy from "./busy.js";
import * as clientlog from "./clientlog.js";
import * as desktop from "./desktop.js";
import { confirmDialog } from "./dialog.js";
import { checkNow, onHealth, startHeartbeat, stopHeartbeat } from "./health.js";
import * as sheet from "./inspection.js";
import * as preview from "./preview.js";
import * as printing from "./printing.js";
import * as running from "./running.js";
import * as logs from "./logs.js";
import * as settings from "./settings.js";
import { toast, toastError } from "./toast.js";

// 押されたボタンを覚えておく。送信を始めたら、そのボタンを待機の姿にする
busy.watchClicks();
// 画面の中で起きたエラーをサーバのログに残す(後追いのため)
clientlog.install();
// デスクトップ版のときだけの部品(フォルダーの選択窓など)を見せる。ブラウザ版では何もしない
desktop.revealDesktopOnly();

let printActive = false;
onHealth((body, reason) => {
  if (!printActive) running.fromHealth(body);
  if (reason === "reconnected") {
    toast("サーバーに再接続しました。", "ok");
    sheet.resync();
    printing.resume();
  }
});
sheet.onChange(() => { printActive = sheet.state.printing; });

// 表に戻ったら、見ていないあいだの変化(別タブでの読み直し・印刷の終わり)を拾う
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { sheet.resync(); printing.resume(); }
});

startHeartbeat();

function ended() {
  stopHeartbeat();
  document.getElementById("ended").hidden = false;
  document.title = "終了しました";
}

// 「終了」ボタン (基盤仕様書 2.8)。印刷中はサーバが 409 を返す。勝手に中断しない
document.getElementById("quit").addEventListener("click", async () => {
  // デスクトップ版は統合ツールの窓の中で動く。終えると、ほかのツールのタブも閉じる
  const message = desktop.isDesktop
    ? "統合ツールを終了しますか?(ほかのツールのタブもまとめて閉じます)"
    : "点検表システムを終了しますか?";
  const ok = await confirmDialog({ title: "アプリの終了", message,
                                   okText: "終了する", danger: true });
  if (!ok) return;
  try {
    const body = await api.post("/api/shutdown", {});
    toast(body.message || "終了します", "ok");
    setTimeout(ended, 500);
  } catch (err) {
    if (err.status === 409) {
      const force = await confirmDialog({ title: "処理の途中です", message: err.message || "終了しますか?",
                                          okText: "中断して終了", danger: true });
      if (!force) return;
      try {
        await api.post("/api/shutdown", { force: true });
        setTimeout(ended, 500);
      } catch (e) { toastError(e); }
      return;
    }
    toastError(err);
  }
});

sheet.init().then(() => printing.resume());
preview.init();
printing.init();
settings.init();
logs.init();
checkNow();
