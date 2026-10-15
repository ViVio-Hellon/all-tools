/*
  theme.js — 画面の明るさ(ライト / ダーク)

  帯の右のボタンで ☀ ライト → ☾ ダーク → ◇ 既定 → … と切り替える(看板 VER3.5.0 と同じ回り方)。
  **選んでいなければ「既定」** ── 日報複合ツールの大設定の「画面の色の既定」に従う
  (大設定が無い・まだ届いていなければライト。4ツールでそろえる)。

  【大設定でダークにしているのにライトで出た】(VER2.4.0)
    > 大設定で ダークにしているのに ライト表示される(大設定の値がデフォルトになっていない)
  前の版は明暗を `dark_mode`(真偽)に覚えていて、ボタンを1度でも押した端末・配布設定で
  配られた端末は「選んだ」扱いのまま、大設定の既定を一切受けなかった。しかも選ばない状態へ
  戻る手段が無かった(ライト ⇄ ダークと回るだけ)。そこで
    ・選んだものは `theme_choice`(light / dark)に覚える。前の版の `dark_mode` はサーバが
      一度だけ移す(ライト = 既定の値は選んだに数えない。settings_service.py の【画面の明るさ】)
    ・ボタンの回り順に「◇ 既定」(選ばない = 大設定に従う)を足した

  選んだものは**サーバ**が覚える(`data/user_settings.json`。ブラウザ版とデスクトップ版で同じ)。
  大設定の既定は、大きなタブの画面から届いたものを**この画面**が覚える
  (localStorage の `inspection.theme_default`)。最初の 1 回は base.html の頭の小さな
  スクリプトが**描く前に**当てる(白く光ってから暗くなるのを防ぐ)。

  実際の色は tokens.css が持つ(`:root[data-theme="dark"]` の色一式)。ここは
  `<html data-theme>` を付け替えるだけ。
*/

import { api } from "./api.js";
import { toastError } from "./toast.js";

/** 日報複合ツールの大設定で決めた「画面の色の既定」。**自分で選んでいないときだけ**効く */
export const SHELL_DEFAULT_KEY = "inspection.theme_default";

/** 選ばない(大設定の既定に従う) */
const DEFAULT_CHOICE = "default";
/** 大設定の既定が無い・まだ届いていないときの明るさ */
const DEFAULT = "light";
/** 選べるもの(サーバが覚えるもの) */
const CHOSEN = ["light", "dark"];
/** ボタンの回り順 */
const ORDER = ["light", "dark", DEFAULT_CHOICE];
const LABEL = { light: "☀ ライト", dark: "☾ ダーク", [DEFAULT_CHOICE]: "◇ 既定" };
const NAME = { light: "ライト", dark: "ダーク", [DEFAULT_CHOICE]: "既定" };

/** いまの選び方(light / dark / default)。サーバが描いた値から始める */
let current = window.APP && CHOSEN.includes(window.APP.theme) ? window.APP.theme : DEFAULT_CHOICE;
/** この画面が開いてから大きなタブの画面に届けられた既定(覚えられない環境でも効くように) */
let received = null;

/** 選んでいないときの明るさ。大設定の既定があればそれ、無ければライト。 */
export function shellDefault() {
  if (received) return received;
  try {
    return localStorage.getItem(SHELL_DEFAULT_KEY) === "dark" ? "dark" : DEFAULT;
  } catch (err) {
    return DEFAULT;
  }
}

/** いまの選び方(light / dark)。選んでいなければ 'default'。 */
export function choice() {
  return current;
}

/** いま実際に使う明るさ(light / dark)。 */
export function effective(c = current) {
  return c === DEFAULT_CHOICE ? shellDefault() : c;
}

/** ボタンを押したら次にどれにするか。「既定」からは**見た目が変わるほう**へ。 */
export function next(c = current) {
  if (c === DEFAULT_CHOICE) return effective(c) === "dark" ? "light" : "dark";
  return ORDER[(ORDER.indexOf(c) + 1) % ORDER.length];
}

function titleOf(c) {
  const now = c === DEFAULT_CHOICE
    ? `既定(日報複合ツールの大設定に従う。いまは${NAME[effective(c)]})`
    : NAME[c];
  const then = next(c);
  const after = then === DEFAULT_CHOICE ? "既定(大設定に従う)" : NAME[then];
  return `画面の明るさ: ${now}。押すと${after}にします`;
}

/** `<html data-theme>` を付け替え、帯のボタンを塗る。 */
export function apply(c = current) {
  const root = document.documentElement;
  root.dataset.theme = effective(c);
  root.dataset.themeChoice = c;
  const btn = document.getElementById("btn-theme");
  if (btn) {
    btn.textContent = LABEL[c] || LABEL[DEFAULT_CHOICE];
    btn.title = titleOf(c);
    btn.setAttribute("aria-label", btn.title);
  }
}

/** 選び方を変えてサーバに覚えさせる。'default' は覚えを消す(大設定の既定に従う)。 */
export async function set(c) {
  const before = current;
  current = CHOSEN.includes(c) ? c : DEFAULT_CHOICE;
  apply();                                  // 先に塗る(押した手応えを待たせない)
  try {
    await api.post("/api/settings", { theme: current });
  } catch (err) {
    current = before;                       // 覚えられなかった。元に戻す
    apply();
    toastError(err);
  }
}

/**
 * 日報複合ツールの大きなタブの画面から「画面の色の既定」が届く(大設定で変えたとき・
 * 枠を開くたび。portal/static/js/shell.js の sendTheme)。覚えておき、**自分で選んで
 * いなければ**その色で塗り直す。
 *
 * **読み込んだ時点で受け付ける**(帯のボタンを繋ぐ `wireThemeToggle` とは切り離す)。
 * 画面の配線(app.js・inspection.js の init)のどこかで止まっても、色の合図は取りこぼさない。
 */
let listening = false;
export function listenShell() {
  if (listening || window.parent === window) return;
  listening = true;
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (event.source !== window.parent) return;
    if (!data || data.type !== "alltools:theme" || (data.theme !== "light" && data.theme !== "dark")) return;
    received = data.theme;
    try { localStorage.setItem(SHELL_DEFAULT_KEY, data.theme); } catch (err) { /* この画面のあいだは効く */ }
    // 選んでいればそのまま(ボタンの説明の「いまは〜」だけ変わる)
    apply();
  });
}
listenShell();

/** 帯のボタンをつなぐ。 */
export function wireThemeToggle() {
  listenShell();
  apply();
  const btn = document.getElementById("btn-theme");
  if (btn) btn.addEventListener("click", () => set(next()));
  // 別の画面(タブ)で大設定の既定を受けたら、こちらも合わせる
  window.addEventListener("storage", (event) => {
    if (event.key !== SHELL_DEFAULT_KEY) return;
    received = null;
    apply();
  });
}
