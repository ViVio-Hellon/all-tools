/*
  theme.js — 画面の見た目(ライト / ダーク / 自動)

  **決めるのはサーバ。** どれが選べるか・いまどれかは `settings.json` の
  「画面の見た目」が持ち、HTML を返す時点で `<html data-theme>` に入っている
  (開くたびに一瞬白く光らないように)。ここは押されたら付け替えて、送るだけ。

  「自動」は属性を付けず、OS(Windows)の設定に任せる(`tokens.css` の
  prefers-color-scheme)。帯のボタンはいま見えている明暗の**反対**へ切り替える。
*/

import { api } from "./api.js";
import { toast, toastError } from "./toast.js";

const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
let current = (window.APP && window.APP.theme) || "auto";
/** この端末で見た目を選んだか。選んでいなければ日報複合ツールの大設定の既定に従う */
let explicit = Boolean(window.APP && window.APP.theme_chosen);
const SHELL_DEFAULT_KEY = "calendar.theme_default";
if (!explicit) {
  try {
    const value = localStorage.getItem(SHELL_DEFAULT_KEY);
    if (value === "light" || value === "dark") current = value;
  } catch (err) { /* 既定のまま */ }
}

/** いま見えている明暗(「自動」なら OS の設定から)。 */
export function effective() {
  if (current === "light" || current === "dark") return current;
  return media && media.matches ? "dark" : "light";
}

/** いまの設定(auto / light / dark)。 */
export function chosen() {
  return current;
}

function apply(theme) {
  current = theme;
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.dataset.theme = theme;
  else delete root.dataset.theme;
  paintToggle();
  document.dispatchEvent(new CustomEvent("app:theme", { detail: { theme } }));
}

/** 見た目を選ぶ。**先に付け替えてから送る**(押した手応えを待たせない)。断られたら戻す。 */
export async function choose(theme, { quiet = false } = {}) {
  const before = current;
  const wasExplicit = explicit;
  apply(theme);
  explicit = true;
  try {
    const result = await api.post("/api/settings/theme", { theme });
    if (!quiet) toast(result.message, "ok");
    return true;
  } catch (err) {
    apply(before);
    explicit = wasExplicit;
    toastError(err);
    return false;
  }
}

function paintToggle() {
  const label = document.getElementById("theme-toggle-label");
  const glyph = document.getElementById("theme-toggle-glyph");
  const button = document.getElementById("theme-toggle");
  if (!label || !glyph || !button) return;
  const dark = effective() === "dark";
  // **押すと何になるか**を書く(いまの状態を書くと、押す意味が逆に読める)
  label.textContent = dark ? "明るくする" : "暗くする";
  glyph.textContent = dark ? "☀" : "☾";
  button.title = dark ? "明るい背景(ライト)に切り替えます" : "暗い背景(ダーク)に切り替えます";
  button.setAttribute("aria-label", button.title);
}

/** 帯のボタンをつなぐ。OS の明暗が変わったら(「自動」のとき)文字も合わせる。 */
export function wireToggle() {
  const button = document.getElementById("theme-toggle");
  if (button) {
    button.addEventListener("click", () => {
      choose(effective() === "dark" ? "light" : "dark", { quiet: true });
    });
  }
  if (media && media.addEventListener) media.addEventListener("change", paintToggle);
  paintToggle();
  // 日報複合ツールの大きなタブの画面から「画面の色の既定」が届く(大設定で変えたとき・
  // 枠を開くたび)。覚えておき、**この端末で選んでいなければ**その色で塗る
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (window.parent === window || event.source !== window.parent) return;
    if (!data || data.type !== "alltools:theme" || (data.theme !== "light" && data.theme !== "dark")) return;
    try { localStorage.setItem(SHELL_DEFAULT_KEY, data.theme); } catch (err) { /* この画面のあいだは効く */ }
    if (!explicit) apply(data.theme);
  });
}
