/*
  asof.js — 「◯時◯分時点」を出し、古くなったら言う

  【なぜ要るのか】
  集計・グラフは `start()` で1度引くだけです。**開きっぱなしにできる
  画面なので、そのまま置いておくと3時間前の数字が今の顔で並びます。**
  VBA では集計シートが「月初〜出力タイミングまで」だったので、いつの
  ものかは印刷した紙の日付で分かりました ── 画面にはそれがありません。

  出すものは2つだけ:

      ◯時◯分時点          いつ引いたか。**常に出す**
      (古くなっています)   直が変わったあと。**そのときだけ**

  【なぜ「◯分以上経ったら古い」にしないのか】
  何分で古いかは場面によります ── 直の途中で見るぶんには10分前でも
  十分ですし、直が変われば1分前の数字でも別の直のものです。**境目が
  はっきりしているのは直の変わり目のほうだけ**なので、そちらを印に
  使い、あとは時刻を出して見た人に任せます。

  判断はサーバが持ちます(`logic/shift_boundary.py`)。ここは受け取った
  ものを出すだけです。
*/

/** この画面の「時点」の置き場所。無い画面もある(日報入力など)。 */
function slots() {
  return [...document.querySelectorAll(".asof[data-at]")];
}

/**
 * サーバが返した時刻を写す。**引き直したら必ず呼ぶ** ── 呼ばないと、
 * 表だけ新しくなって時刻が前のままになります(いちばん悪い形)。
 *
 * @param {string} at "HH:MM"。空なら何もしない
 */
export function stampAsOf(at) {
  if (!at) return;
  for (const el of slots()) {
    el.dataset.at = at;
    el.textContent = `${at}時点`;
    delete el.dataset.stale;
  }
}

/**
 * 直が変わったので、この数字はもう別の直のもの、と出す。
 *
 * **消しません。** 引き直せば `stampAsOf` が消します ── 見ているあいだに
 * 勝手に消えると、読んだ覚えだけが残って、古い数字を新しいものとして
 * 扱うことになります。
 */
export function markStale(stale = true) {
  for (const el of slots()) {
    const at = el.dataset.at || "";
    if (!stale) { delete el.dataset.stale; el.textContent = `${at}時点`; continue; }
    if (el.dataset.stale === "1") continue;
    el.dataset.stale = "1";
    el.textContent = `${at}時点（直が変わりました。引き直してください）`;
  }
}
