/*
  theme.js — 背景をライト / ダークから選ぶ (v4.19.0)

  > ダークモード ライトモードで背景チェンジできるようにしてください

  【色はもう両方ある】
  `tokens.css` は前から3つの状態を持っています ── ライト(既定)・OS がダーク・
  `<html data-theme="dark|light">`(選んだとき)。ここは**どちらを選んだか**を
  `<html>` に付けるだけで、色の値には触りません。

  【選ぶまではライト】
  まだ一度も選んでいなければ**ライト**です(統合ツールの4ツールでそろえる。
  以前は OS(Windows のダークモード)に従っていて、ツールごとに既定がバラバラだった)。
  帯のボタンは、いま効いているほうを押された見た目にします。

  【覚えるのは端末ごと】
  見た目の好みは共有DBに書くものではないので `localStorage` に置きます
  (グラフのネオン `skin.js` と同じ)。読めない場面があるので、読み書きは
  必ず try/catch。**描く前に当てる**のは `base.html` の `<head>` の小さな
  script です(あとから付けると一瞬白く光る)。

  【コイル・平板(枠の中の別の道具)も揃える】
  VC長さ計算の「コイル・平板」は、自分の切り替え(`vc/coil/coil-theme.js`)を
  持つ別の道具です。こちらで選んだら、あちらの覚え(`vc-calculator.coil.theme`)
  にも同じものを書き、開いていればあちらのボタンを押して塗り直させます。
*/

const KEY = "nippou.theme";
/** コイル・平板の道具が覚えている場所(`vc/coil/coil-theme.js` の KEY) */
const COIL_KEY = "vc-calculator.coil.theme";
const THEMES = ["light", "dark"];

/** 選んだもの。まだ選んでいなければ null。 */
export function saved() {
  try {
    const value = localStorage.getItem(KEY);
    return THEMES.includes(value) ? value : null;
  } catch (err) {
    return null;
  }
}

/** 選んでいないときの見た目 */
const DEFAULT = "light";

/** いま効いているほう。 */
export function current() {
  return saved() || DEFAULT;
}

/** 帯のボタンの押された見た目を、いま効いているほうに合わせる。 */
function paintButtons() {
  const now = current();
  for (const btn of document.querySelectorAll("[data-theme-choice]")) {
    btn.setAttribute("aria-pressed", btn.dataset.themeChoice === now ? "true" : "false");
  }
}

/** コイル・平板(枠の中)を揃える。開いていなければ覚えだけ書く。 */
function syncCoil(theme) {
  try { localStorage.setItem(COIL_KEY, theme); } catch (err) { /* 覚えられなくてもよい */ }
  for (const frame of document.querySelectorAll("iframe")) {
    try {
      const doc = frame.contentDocument;
      const btn = doc && doc.querySelector(`.theme-btn[data-theme-choice="${theme}"]`);
      if (btn) btn.click();            // あちらの切り替えで、3D とグラフまで塗り直す
    } catch (err) { /* 別の出どころの枠は触らない */ }
  }
}

/**
 * 当てる。**`<html>` の印を変えるだけ** ── 色の値は CSS が持っています。
 * @param {"light"|"dark"} theme
 */
export function apply(theme) {
  if (!THEMES.includes(theme)) return;
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(KEY, theme);
  } catch (err) {
    /* 覚えられなくても、この画面のあいだは効いています */
  }
  paintButtons();
  syncCoil(theme);
}

/**
 * 帯の切り替えを繋ぐ。**帯は画面を移るたびに作り直される**ので、そのたびに
 * 呼ばれます(同じボタンに二重に付けないよう、付けたボタンに印を残す)。
 */
export function wire(box) {
  if (!box) return;
  for (const btn of box.querySelectorAll("[data-theme-choice]")) {
    if (btn.dataset.wired === "1") continue;
    btn.dataset.wired = "1";
    btn.addEventListener("click", () => apply(btn.dataset.themeChoice));
  }
  paintButtons();
}

