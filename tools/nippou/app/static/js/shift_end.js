/*
  shift_end.js — 直の終わりまわりの1分タイマー

  VBA は `Application.OnTime` で1分ごとに `CheckPrintReminder` を回し、

      残り15分  印刷忘れの催促(ラベル + 音 + 5分おきにポップアップ)
      残り 5分  **印刷処理を自動で走らせる**(`Print保存` + `Aggre保存` + 印刷)
      直の終わり 実績のグラフを最大化して出す(`graphF`)

  をやっていました。ブラウザには `OnTime` が無いので、その役を
  **画面の1分タイマー**が担います。ここは時計を持つだけ ── いつ何をするかは
  全部サーバが決めます(`logic/shift.py` / `logic/shift_review.py` /
  `services/shift_close.py`)。画面に判断を置くと、タブごとに違う答えを
  出しかねません。

  残り5分にやることは**「印刷」から「確定」に変えました** ── 紙は作業者が
  欲しいときだけ「印刷」ボタンから出すものにしたので、直の終わりまでに
  済ませたいのはチェックと集計のほうです。

  【1分に3つ、順番が要る】
      1. 直が変わっていないか (GET /api/shift/key)
      2. 自動確定 (POST /api/entry/close)
      3. 確認画面へ連れて行くか (GET /api/graph/review)

  **確定は確認より先**です。逆にすると、確認画面が「確定前の集計」を
  映したまま遷移してしまい、同じ直の数字が画面ごとに食い違って見えます。

  【なぜ直の見張りがここに居るのか】
  VBA は保存処理の最後で `Unload UFdaily` していたので、**フォームが
  直の変わり目をまたいで生き残ることがまずありませんでした。**
  開きっぱなしにできる Web版は、17:00 を過ぎても「1直」と表示したまま
  2直へ書きます。

  押すまで気づけないのでは遅すぎます ── 17:00 に手を止めて休憩に入り、
  17:20 に戻って保存を押す、という場面で、20分ぶんの誤解が積み上がる
  からです。**1分ごとに自分から確かめて**、帯を塗り直し、消えない
  知らせを出します。

  ここも**判断はしません。** またいだかどうかを決めるのはサーバの
  `logic/shift_boundary.py` で、こちらは受け取ったものを出すだけです。

  【紙は1枚も出ません】
  残り5分でやるのはチェック → 集計の作り直し → 締めた印、まで。
  紙が要るときは「印刷」ボタンから、欲しいぶんだけ出します。

  【音を鳴らすのは `sound.js`】
  催促の音は別の見張りが持っています。ここでは鳴らしません。
*/

import { background } from "./api.js";
import { markStale } from "./asof.js";
import { refresh } from "./nav.js";
import { paintRibbon, showShiftMoved } from "./ribbon.js";
import { toast } from "./toast.js";

// 訊きに行く間隔(ms)。VBA のタイマーが1分ごとだったので合わせる
const POLL_MS = 60000;

const REVIEW_URL = "/graph/review";

/**
 * この画面が**自分を何の直だと思っているか**。
 *
 * 決まりは1つ、`data-opened-date` を持つ要素です ── 日報入力なら
 * `#sheet-key`、集計・グラフなら「◯時◯分時点」の札。画面が自分で
 * 覚えると、覚えたほうとサーバが描いたほうが食い違うので、**出どころは
 * いつでもサーバが描いた属性**にします。
 *
 * 名乗らない画面(設定など)は `null`。直の変わり目を気にする必要が
 * ないので、見張りも何も出しません。
 */
function openedKey() {
  const el = document.querySelector("[data-opened-date]");
  if (!el) return null;
  return {
    report_date: el.dataset.openedDate || "",
    shift: el.dataset.openedShift || "",
    page: el.dataset.openedPage || "1",
  };
}

/**
 * 直が変わっていないか、1分ごとに自分から確かめる。
 *
 * ついでにサーバ側で `before_request` の `_roll_over` が走るので、
 * **誰も触っていなくても管理者モードが1分以内に落ちます**
 * (`app/__init__.py`)。
 */
async function shiftWatch() {
  const opened = openedKey();
  const query = opened
    ? `?report_date=${encodeURIComponent(opened.report_date)}`
      + `&shift=${encodeURIComponent(opened.shift)}`
      + `&page=${encodeURIComponent(opened.page)}`
    : "";
  try {
    const body = await background.get(`/api/shift/key${query}`);
    // **帯は常に塗り直す。** またいでいなくても、ページが増えていることが
    // ある(別のタブで新しいページを出した、など)
    paintRibbon(body.ribbon);
    const moved = body.shift_changed;
    // **固定した直の知らせを優先する。**
    //
    // 作業者を選んだ時点で書き先の直は決まっているので(`shift_anchor`)、
    // 時計が 15:00 を越えても書き先は動きません ── 「直が変わりました」
    // ではなく「1直は終わっています。この画面は見るだけです」のほうが、
    // いま起きていることです。
    const standing = body.anchor_standing || {};
    if (standing.message) showShiftMoved(standing.message);
    else showShiftMoved(moved && moved.crossed ? moved.message : "");
    // **見るだけになった瞬間は塗り直す。** サーバはもう打たせない答えを
    // 返しますが、画面は打てるままです ── 打てるのに保存だけ断られる、が
    // 一番分かりません。
    //
    // ここだけは**打っている最中でも塗り直します。** 見送る作りにして
    // いたら、欄に文字入れを置いたまま立ち去った画面が閉じませんでした
    // ── 閉じないと、次に座った人がその紙の続きを打つことになります。
    // 直はもう終わっていて、サーバは保存も断っているので、打ちかけを
    // 残しても行き先がありません。
    if (standing.expired && document.getElementById("read-only")?.hidden) {
      refresh();
    }
    // 集計・グラフの数字は、もう別の直のもの。**引き直すまで印を残す**
    if (moved) markStale(moved.crossed);
  } catch (err) {
    // 繋がらないのは `health.js` が知らせる。ここでは黙る
  }
}

/**
 * 残り5分の自動確定をサーバに促す。
 *
 * **ほとんどの回は「まだ早い」で返ります。** それは失敗ではないので
 * 黙って終わります。走った回だけ、何をしたかを帯で知らせます ──
 * 勝手に確定されたことに気づかないほうが困るので。
 */
async function autoConfirm() {
  try {
    const body = await background.post("/api/entry/close", {});
    // **拾ったぶんも知らせる。** サーバは「いまの直(`ran`)」のほかに
    // **終わったのに締まっていない直**もあとから締めます(`swept`) ──
    // 17:00 に終わった直は、17:05 に開いた画面からは「まだ早い」に
    // しかならず、二度と確定されないままでした
    const swept = body.swept || [];
    if (!body.ran && !swept.length) return;
    toast(body.message || "打ってあるぶんを確定しました", "ok");
    // **走ったら画面を塗り直す。**
    //
    // 画面は HTML が描かれた時点の答えを持っています。確定も拾いも
    // そのあとなので、**共有へ送り終えたのに「共有へ未送信 ◯直ぶん」や
    // 「前の直が共有へ出ていません / 入力は始められません」が出たまま**に
    // なります ── 押す物も無いのに止まって見えて、開き直すまで
    // 抜けられません。
    //
    // 塗り直すのは `ran`(いまの直を確定した)でも同じです。**片方だけ
    // 直すと、同じ症状が残る側と消える側ができます。**
    refreshUnlessTyping();
  } catch (err) {
    // 繋がらないのは `health.js` が知らせる。ここでは黙る
  }
}

/**
 * 塗り直す。ただし**打っている最中なら見送る。**
 *
 * `refresh()` は本文を差し替えるので、入力中の欄は書きかけが消えます。
 * 拾いが効くのは「まだ打てない画面」(前の直で止まっている)なので、
 * 打っている人の手を止めてまで塗り直す必要はありません ── そちらは
 * 次に保存したときの応答で塗り替わります。
 */
function refreshUnlessTyping() {
  const here = document.activeElement;
  const tag = (here && here.tagName) || "";
  if (["INPUT", "SELECT", "TEXTAREA"].includes(tag)) return;
  refresh();
}

//: 連れて行かない画面。**設定・管理者は「直す場所」**です
//
// 全画面の確認は入力の手を止めるためのものなので、入力や記録から連れて
// 行くのは狙いどおりです。**設定だけは別**でした ── パスを直している
// 最中、過去データを取り込んでいる最中、中身の無い紙を片付けている
// 最中に連れ去られると、その作業ができません。
//
// 実際、中身の無い紙を片付ける画面を開いた瞬間に連れて行かれました
// ── **片付けようとしている当人が、片付ける画面に居られない。**
//
// 待たせても困りません。設定を閉じて入力へ戻れば、そこで出ます。
const KEEP_HERE = ["/settings"];

/** 実績の確認画面を出すべきか訊いて、そうなら連れて行く。 */
async function reviewCheck() {
  // すでに確認画面に居るなら何もしない(自分自身へ飛ばさない)
  if (location.pathname === REVIEW_URL) return;
  if (KEEP_HERE.some((path) => location.pathname.startsWith(path))) return;
  try {
    const body = await background.get("/api/graph/review");
    if (body.due) location.href = REVIEW_URL;
  } catch (err) {
    // 同上
  }
}

async function tick() {
  // **直の見張りが先。** 変わっていたら、そのあとの2つより先に帯へ出す
  await shiftWatch();
  // **確定 → 確認** の順。確認画面には確定後の数字を映したい
  await autoConfirm();
  await reviewCheck();
}

export function startShiftEndWatch() {
  document.getElementById("shift-moved-reload")
    ?.addEventListener("click", () => location.reload());
  // タブに戻ってきた・スリープから起きた瞬間も確かめる。**放置していた
  // あいだに直が変わっていることがあり**、次の周期まで古い直のまま操作
  // できてしまう。戻ったことに気づくのは `health.js`(`app:resume`)──
  // 表に戻った・凍結が解けた・スリープ明けを1か所で拾っている
  window.addEventListener("app:resume", () => shiftWatch());
  tick();
  setInterval(tick, POLL_MS);
}
