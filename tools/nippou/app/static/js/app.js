/*
  app.js — 全画面で共通の起動処理

  画面ごとの中身は views/ 以下。ここは外枠の面倒だけを見る。

  【ここは入口。**どこからも import しない。**】
  このファイルはトップレベルで副作用を持つ(心拍・音の見張り・
  差し替えの開始)。`<script src>` から1回だけ読まれる前提です。

  以前は `api` / `toast` / `toastError` をここから再輸出していて、
  views が `../app.js` を import していました。ところが `<script>` は
  版付きの `app.js?v=…` を読み、views の相対 import は版なしの
  `app.js` を読むので、**別のURL = 別のモジュールとして2回評価され**、
  副作用が全部2度走っていました:

      ・nav.start() が2回 → document のクリック listener が2つ
      ・nav.mountViews() が2回 → **どのボタンにも listener が2つ**
        (押すと2回送る・トーストが2つ出る・確認が2回出る)
      ・心拍と音の見張りも2つずつ

  そこで再輸出をやめました。views は `api.js` / `toast.js` から
  直に取ります ── **入口を import しない**のが約束です。
*/

import { api } from "./api.js";
import { startErrorReport } from "./errors.js";
import { startHeartbeat } from "./health.js";
import { startHints } from "./hint.js";
import { startShiftEndWatch } from "./shift_end.js";
import { startSoundWatch } from "./sound.js";
import { toast, toastError } from "./toast.js";
import * as busy from "./busy.js";
import * as desktop from "./desktop.js";
import * as nav from "./nav.js";
import * as ribbon from "./ribbon.js";
import * as theme from "./theme.js";

/* ================================================================
   いま動いているのはどの版か

   **HTMLの版表示だけでは足りない。** HTMLは毎回サーバが作り直すので
   いつでも新しく見えますが、JS はブラウザが控えているかもしれません
   ── 「入れ替えたのに古いまま」は、そこで起きます。

   このファイルは `<script src="app.js?v=<印>">` から読まれるので、
   **自分の URL に印が入っています**(`import.meta.url`)。サーバが
   HTML に埋めた印(`window.APP.stamp`)と突き合わせれば、
   *画面* が古いかどうかがその場で分かります。

   合わなければ黙って直せる話ではない(控えを捨てるのは利用者の操作)
   ので、帯の版表示に印を付けて、やることを画面に出します。
   ================================================================ */

/** 自分が読まれた URL に入っている印。取れなければ空。 */
export function runningStamp() {
  try {
    return new URL(import.meta.url).searchParams.get("v") || "";
  } catch (err) {
    return "";
  }
}

function checkVersion() {
  const served = (window.APP && window.APP.stamp) || "";
  const running = runningStamp();
  // 設定画面が「画面(JS)の印」として出すので、置いておく。
  // **import させない** ── ここは入口なので、他から読むと2回評価される
  if (window.APP) window.APP.runningStamp = running;
  // 印が取れない場面(古いブラウザ・直接開いた等)は黙って通す。
  // **分からないことを理由に警告を出さない**
  if (!served || !running || served === running) return;

  console.warn("[app] 画面が古い可能性:", running, "≠", served);
  const badge = document.getElementById("app-version");
  if (badge) {
    badge.dataset.stale = "1";
    badge.title = `画面が古い可能性があります(画面 ${running} / サーバ ${served})`;
  }
  toast("画面が古いままです。Ctrl+Shift+R で読み直してください。", "warn");
}

// **押されたボタンを覚えておく。** `api.js` が送信を始めるときに、
// そのボタンを待機の姿にする(無言で待たせない)
busy.watchClicks();
// 画面のエラーを記録へ届ける(直前に押したものを添えて)。**いちばん先に** ──
// このあとの起動で転んだものも拾うため
startErrorReport();
// デスクトップ版(日報複合ツールの窓)のときだけ、別窓・フォルダ選択を外枠に頼む
desktop.install();
checkVersion();

startHeartbeat();
// 音の出番を訊きに行く。**鳴らす判断はサーバ**(`logic/sound.py`)
startSoundWatch();
// 直の終わりの1分タイマー(VBA `Application.OnTime` 相当)。
// 残り5分の自動確定と、実績の確認画面への誘導をこれが回す
startShiftEndWatch();
// マウスを乗せると簡易説明(`data-hint`、v4.7.0)。document に1度だけ付ける
startHints();

/**
 * リボンの中の道具を繋ぐ。
 *
 * **リボンは画面ごとに作り直される**(値が画面で違う)ので、差し替えの
 * たびに呼ばれる。同じ要素へ二重に付かないよう、付けた要素に印を残す
 * ── 押すたびに2回終了しようとしないため。
 */
function wireShell() {
  wireQuit(document.getElementById("quit"));
  wireRibbonChips(document.getElementById("rb-chips"));
  // 背景のライト / ダーク(v4.19.0)。帯と一緒に作り直されるので、ここで繋ぐ
  theme.wire(document.getElementById("theme-switch"));
}

/*
  帯の印を押して、そのモードを終わる。

  「過去データの終了方法 / 管理者モードの終了方法 これらがわからない」と
  言われたところです。どちらも入っていることは帯に出していましたが、
  **やめる場所は別の画面の奥**にありました。状態の隣に出口を置きます。

  どちらも**確かめてから**。押し間違いで過去データを閉じると、直していた
  内容がどこへ行ったのか分からなくなります(サーバは閉じる前に保存します
  が、押した人にはそう見えません)。

  終わったら**画面を引き直します** ── 過去データを閉じると開く紙が
  変わるので、値の入った古い画面を残せません。
*/
function wireRibbonChips(box) {
  if (!once(box)) return;
  ribbon.wireChips(box, {
    /*
      **閉じる前に、直していたぶんを置く**(v4.24.0)。

      ここは `/api/settings/back` へ空のまま送っていたので、サーバは何も保存
      せずに呼び出しを解いていました ──「閉じる前に、開いていたぶんは保存
      されます」と言いながら、直した値は消え、しかも閉じる間際の送信が
      **直していたページの中身を、戻った先の最新のページへ**書いていました。
      先に日報入力の打ちかけを置き(`nav.leaving()`)、済んだら「書いたもの」に
      してから移ります(`nav.markSaved()`)。
    */
    back: async (asked = "") => {
      if (!confirm(asked || ("過去データを閉じて、いまの直の入力に戻ります。"
                   + "\n(閉じる前に、開いていたぶんは保存されます)"))) return;
      if (!(await nav.leaving())) return;
      try {
        const body = await api.post("/api/settings/back", {});
        toast(body.message || "作業に戻りました", "ok");
        nav.markSaved();
        location.href = "/";
      } catch (err) { toastError(err); }
    },
    // 管理者モードを終える前にも置く。**外れたあとでは、呼び出した過去の直へは
    // 書けません**(見るだけになる)── 直した値が読み直しで消えていました
    adminOff: async () => {
      if (!confirm("管理者モードを終わります。よろしいですか?")) return;
      if (!(await nav.leaving())) return;
      try {
        await api.post("/api/settings/admin", { enable: false });
        toast("管理者モードを終わりました", "ok");
        nav.markSaved();
        location.reload();
      } catch (err) { toastError(err); }
    },
  });
}

/*
  動線の帯の「ここへ」── そのボタンまで連れて行く。

  次にやることがこの画面にあっても、**長い画面では下のほうにあります**。
  「保存(確定)はどこ?」で止まるくらいなら、帯から運ぶほうが速い。
  押した先を少し光らせるのは、着いたことが分かるようにするため。

  **要素ごとではなく document で受けます。** 帯は打つたびに描き直され
  (`views/entry.js:paintFlow`)、ボタンが作り直されるので、要素に付ける
  やり方だと「打ったあとだけ効かない」が起きます。
*/
document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-goto]");
  if (!btn) return;
  const target = document.getElementById(btn.dataset.goto);
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.dataset.pointed = "1";
  setTimeout(() => { delete target.dataset.pointed; }, 2000);
  // 焦点も移す ── キーボードで進める人を置いていかない
  try { target.focus({ preventScroll: true }); } catch (err) { /* 無視 */ }
});

function once(node) {
  if (!node || node.dataset.wired === "1") return false;
  node.dataset.wired = "1";
  return true;
}

// 「終了」ボタン (基盤仕様書 2.8)。
// 実行中の処理があるときサーバは 409 を返す。勝手に中断しない
function wireQuit(button) {
  if (!once(button)) return;
  button.addEventListener("click", async () => {
    // 日報入力の打ちかけを先に置く(置けずに「やめる」を選ばれたら終わらない)
    if (!(await nav.leaving())) return;
    try {
      const body = await api.post("/api/shutdown", {});
      toast(body.message || "終了します", "ok");
      setTimeout(() => {
        // デスクトップ版は外枠が窓を閉じる(ほかのタブのツールにも訊いてから)
        const after = desktop.isDesktop
          ? "<h1>終了しています…</h1><p>窓はこのあと閉じます。</p>"
          : "<h1>終了しました</h1><p>このタブは閉じてかまいません。</p>";
        document.body.innerHTML =
          '<main style="padding:48px;font-family:var(--sans)">' + after + "</main>";
      }, 700);
    } catch (err) {
      if (err.status === 409) {
        // 中断してよいかは利用者が決める
        if (confirm("実行中の処理があります。中断して終了しますか?")) {
          try {
            await api.post("/api/shutdown", { force: true });
            toast("終了します", "ok");
          } catch (e) { toastError(e); }
        }
        return;
      }
      toastError(err);
    }
  });
}

// 画面切替のキーボード操作。現場は片手作業が多い
document.addEventListener("keydown", (event) => {
  if (event.altKey || event.ctrlKey || event.metaKey) return;
  // 入力中の F キーは画面切替に使わない(12行グリッドの連続入力を邪魔しない)
  const tag = (event.target?.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  const match = /^F([1-7])$/.exec(event.key);
  if (!match) return;
  const link = document.querySelectorAll(".rail a")[Number(match[1]) - 1];
  if (link) { event.preventDefault(); link.click(); }
});

wireShell();
/*
  **日報複合ツールの窓を閉じる前に、打ちかけを置く。**

      デスクトップ版の窓の×・終了では、日報の打ちかけはまだ保存されません:
      保存するようにしてください

  大きなタブの画面(外枠)が、Python に「終わってよいか」を訊く前に頼んで
  きます(`alltools:before-close`)。すぐ「受けた」を返し、日報入力なら
  打ちかけを置いて(置けなければ閉じてよいかを訊いて)「済んだ」を返します。
  頼んでくるのは、この画面を埋め込んでいる親だけ(`window.parent`)。
*/
// **外枠に「この画面は閉じる前の頼みに自分で答えます」と名乗る**(ほかのツールと
// 同じ決まり)。名乗らないと、外枠の受け皿(`embed.js`)も同じ頼みに答えて、
// 2重に訊きます
window.__alltoolsHandlesClose = true;
window.addEventListener("message", async (event) => {
  const data = event.data;
  if (event.source !== window.parent || window.parent === window) return;
  if (!data || data.type !== "alltools:before-close") return;
  const reply = (type, extra = {}) => {
    try { window.parent.postMessage({ type, seq: data.seq, ...extra }, event.origin); }
    catch (err) { /* 親がもう居ない */ }
  };
  reply("alltools:before-close-ack");
  let ok = true;
  try { ok = await nav.leaving(); } catch (err) { ok = true; }
  // 日報入力のほか(VC計算・梱包資材・設定)は、出る前の確かめを持ちません。
  // **名乗った以上、外枠の受け皿はもう訊かない**ので、打った欄があればここで訊く
  try {
    if (ok && !nav.guarded() && nav.typedHere()) {
      ok = confirm("保存していない入力があります。閉じますか?\n\n"
                   + "(閉じると、この画面で打った値は消えます)");
    }
  } catch (err) { /* 訊けなければ閉じてよい */ }
  reply("alltools:before-close-done", { ok });
});

nav.start({ onSwap: wireShell });
// 画面ごとのモジュールを繋ぐ。**最初の1回もここから。** 差し替えのときと
// 同じ道を通しておかないと、「初回だけ効く」ものが出てくる
nav.mountViews();

// ここまで来たら外枠は繋がっている。`base.html` の見張りを解く ──
// **module は転んでも何も言わない**ので、立たなければ向こうが画面に出す
if (window.APP) window.APP.booted = true;
