/*
  skin.js — グラフの見た目を切り替える(ネオン / 元のまま)

  【なぜ切り替えられるのか】
  > ネオンサイバーパンク風の鮮やかなビジュアルに変更してください
  > onoffができ修正前のものと切り替えれるようにしておいて

  見た目の好みは人と場所で分かれます。昼の明るい事務所と、夜の現場の
  端末では、読みやすい絵が違います ── **どちらかに決めてしまわない**で、
  押した人が選べるようにします。

  【前の姿を別に持ちません】
  ネオンは `tokens.css` / `components.css` の**上書きの層**です
  (`:root[data-skin="neon"]` の中だけ)。外せば下にある元の値がそのまま
  出るので、「前の見た目」を2つ目の定義として抱える必要がありません
  ── 2つ持つと、片方だけ直す日が必ず来ます。

  【当たるのは集計・グラフだけ】
  「あくまで集計結果とグラフに関してだけです」── ネオンの規則は
  `.chart-skin`(グラフを描くところ)と `.tile`(集計の数字カード)にしか
  書いていません。日報入力もGW計算も、これまでどおりです。

  【覚えるのは端末ごと】
  見た目の好みは、共有DBに書くようなものではありません。`localStorage`
  に置いて、その端末のブラウザだけが覚えます。読めない場面
  (プライベート窓・記憶を消した直後)があるので、**読み書きは必ず
  try/catch**で、失敗したら既定(ネオン)に倒します。
*/

const KEY = "nippou.skin";
const NEON = "neon";
const PLAIN = "plain";

/** 覚えている選択。読めなければ既定(ネオン)。 */
export function current() {
  try {
    return localStorage.getItem(KEY) === PLAIN ? PLAIN : NEON;
  } catch (err) {
    return NEON;
  }
}

/** いま当たっているか。 */
function isNeon() {
  return document.documentElement.dataset.skin !== PLAIN;
}

/**
 * 当てる。**`<html>` の印を変えるだけ** ── 色の値はCSSが持っています。
 *
 * @param {boolean} on
 */
export function apply(on) {
  document.documentElement.dataset.skin = on ? NEON : PLAIN;
  try {
    localStorage.setItem(KEY, on ? NEON : PLAIN);
  } catch (err) {
    /* 覚えられなくても、この画面のあいだは効いています */
  }
}

/**
 * 切り替えのつまみを繋ぐ。**画面を移るたびに呼びます**(つまみは
 * 差し替えで作り直されるので)。
 */
export function wire(box) {
  if (!box) return;
  box.checked = isNeon();
  box.addEventListener("change", () => apply(box.checked));
}
