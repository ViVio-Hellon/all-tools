/*
  leave.js — 打ちかけを黙って失わない

  【何が起きていたか】
  連絡の本文・設定の欄・マスタの行は、**保存するまで画面の中にしか無い。**
  それなのに、次のどれでも何も聞かずに消えていた:

    * サーバが起動し直された(夜の自動終了→朝の起動・版の更新)のを
      見張り(`health.js`)が見つけて、黙って `location.reload()`
    * 起動トークンが古くて断られた要求(`api.js`)が、同じく黙って読み込み直し
    * 日報複合ツールの窓の × ・「終了」(外枠からの `alltools:before-close` に
      答えていなかったので、外枠は待たずに閉じた)
    * タブを閉じる・別の画面へ移る(`beforeunload` を出していなかった)

  「打った行が消えることがありました」── とんでもない話である。

  【どうするか】
  打ちかけを持つ画面が `register()` で「いま保存していないもの」を
  言えるようにしておき、消える前に**ここ1か所で**訊く。
  連絡の本文はこれとは別に `sessionStorage` へ置いてあるので
  (`views/calendar.js`)、読み込み直しても戻る ── 訊くのは
  戻せないもの(設定の欄・マスタの行)を失う前の念押し。
*/

import { confirm } from "./modal.js";

const holders = new Set();
// 読み込み直す・閉じると決めたあと。**もう訊かない**(二重に訊くと押した側が迷う)
let leaving = false;
let asking = null;
// 「あとで」と言われたか。**裏の問い合わせ(数秒ごと)では訊き直さない** ──
// 毎回出すと入力を続けられない。訊き直すのは保存を押したときだけ
let declined = false;

/**
 * 「保存していないもの」を言う関数を足す。言うことが無ければ空文字か null を返す。
 * @param {() => (string|null|undefined)} fn
 * @returns {() => void} 外す
 */
export function register(fn) {
  holders.add(fn);
  return () => holders.delete(fn);
}

/** いま保存していないものの説明(無ければ空の配列)。 */
export function unsaved() {
  const out = [];
  holders.forEach((fn) => {
    try {
      const text = fn();
      if (text) out.push(String(text));
    } catch (_err) { /* 1つ転んでも他は数える */ }
  });
  return out;
}

function listOf(items) {
  return items.map((text) => `・${text}`).join("\n");
}

/**
 * 読み込み直す。**保存していないものがあれば先に訊く。**
 *
 * 「あとで」を選ばれたら読み込み直さない(入力を続けてもらう)。
 * 保存しようとして断られたら、そのときもう一度ここへ来る(`api.js`)。
 * @param {string} why 読み込み直す理由(画面に出す)
 * @param {{byUser?: boolean}} [opts] 押した操作から来たか(裏の問い合わせなら false)
 * @returns {Promise<boolean>} 読み込み直すなら true
 */
export function reloadSafely(why, { byUser = false } = {}) {
  if (leaving) return Promise.resolve(true);
  const items = unsaved();
  if (!items.length) {
    // 打ちかけが片付いた(保存した・閉じた)。ここで読み込み直す
    leaving = true;
    location.reload();
    return Promise.resolve(true);
  }
  if (asking) return asking;
  if (declined && !byUser) return Promise.resolve(false);
  asking = confirm(
    "画面を読み込み直す必要があります",
    `${why}\n\n保存していない入力があります:\n${listOf(items)}\n\n`
    + "連絡の本文は、読み込み直したあとに戻します。設定やマスタの欄は"
    + "戻せないので、先に保存したい場合は「あとで」を押してください"
    + "(保存しようとすると、もう一度ここで訊きます)。",
    { okLabel: "読み込み直す", cancelLabel: "あとで" })
    .then((ok) => {
      asking = null;
      if (ok) {
        leaving = true;
        location.reload();
      } else {
        declined = true;
      }
      return ok;
    });
  return asking;
}

/**
 * 閉じてよいか(日報複合ツールの窓の × ・終了)。保存していないものがあれば訊く。
 * @param {{remember?: boolean}} [opts] 「閉じる」と決めたことを覚えるか
 *        (覚えると、このあと読み込み直す・閉じるときにもう訊かない)
 * @returns {Promise<boolean>}
 */
export async function mayClose({ remember = true } = {}) {
  if (leaving) return true;
  const items = unsaved();
  if (!items.length) return true;
  const ok = await confirm(
    "保存していない入力があります",
    `${listOf(items)}\n\n閉じると、この入力は消えます。閉じますか?`,
    { okLabel: "閉じる(入力は消えます)", cancelLabel: "閉じない", danger: true });
  if (ok && remember) leaving = true;
  return ok;
}

/** タブを閉じる・別の画面へ移る前に、ブラウザの確認を出させる。 */
export function wireBeforeUnload() {
  window.addEventListener("beforeunload", (event) => {
    if (leaving || !unsaved().length) return;
    event.preventDefault();
    // 古いブラウザは returnValue が要る(文言は出ない。ブラウザ決まりの文が出る)
    event.returnValue = "";
  });
}
