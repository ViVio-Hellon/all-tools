// 画面の明るさ(ライト / ダーク)
//
// 帯の右のボタンで ☀ ライト → ☾ ダーク → ◐ 自動 → ◇ 既定 → … と切り替える。
// **選んでいなければ「既定」** ── 日報複合ツールの大設定の「画面の色の既定」に従う
// (大設定が無い・まだ届いていなければライト。4ツールでそろえる)。
// 「自動」は Windows の設定(アプリのモード: ライト / ダーク)に合わせる。
//
// **選んだものはこの端末(この画面)に覚えさせる**(localStorage)。PC ごとの見やすさの
// 好みなので、共有フォルダの設定には入れない。覚えられない環境でも動く。
//
// 【大設定の既定に付いていかなかった】(資材発注看板システム VER3.4.0)
// 前の版は帯のボタンを 1 度でも押すと選んだことになり、**選ばない状態へ戻る手段が
// 無かった**(ライト → ダーク → 自動 → ライト … と回るだけ)。しかも大設定の既定が
// できる前 ── 既定が Windows の設定だったころ ── に「ライト」へ逃げた端末は、その
// 選択が残って大設定の既定を一切受けなかった。そこで
//   ・選んだものを覚える場所を `kanban.theme_choice` に移し、前の版の `kanban.theme`
//     は**一度だけ捨てる**(大設定の既定が無かったころの選択なので)
//   ・ボタンの回り順に「◇ 既定」(選ばない = 大設定に従う)を足した
//
// 実際の色は tokens.css が持つ(`:root[data-theme="dark"]` の色一式)。ここは
// `<html data-theme>` を付け替えるだけ。最初の 1 回は base.html の頭の小さな
// スクリプトが**描く前に**付ける(白く光ってから暗くなるのを防ぐ)。同じ決め方を
// 起動待機画面(kanban/boot_screen.py)も持つ。

/** 自分で選んだもの(light / dark / auto)。無ければ「既定」 */
export const THEME_KEY = 'kanban.theme_choice';
/** 前の版が覚えていた場所。読まずに一度だけ消す(上の説明) */
export const LEGACY_THEME_KEY = 'kanban.theme';
/** 日報複合ツールの大設定で決めた「画面の色の既定」。**自分で選んでいないときだけ**効く */
export const SHELL_DEFAULT_KEY = 'kanban.theme_default';

/** 選ばない(大設定の既定に従う) */
const DEFAULT_CHOICE = 'default';
/** 大設定の既定が無い・まだ届いていないときの明るさ */
const DEFAULT = 'light';
/** 選べるもの(覚えるもの) */
const CHOSEN = ['light', 'dark', 'auto'];
/** ボタンの回り順 */
const ORDER = ['light', 'dark', 'auto', DEFAULT_CHOICE];
const LABEL = { light: '☀ ライト', dark: '☾ ダーク', auto: '◐ 自動', [DEFAULT_CHOICE]: '◇ 既定' };
const NAME = { light: 'ライト', dark: 'ダーク', auto: '自動(Windows の設定に合わせる)', [DEFAULT_CHOICE]: '既定' };

const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

// 前の版の選び方を一度だけ捨てる
try { localStorage.removeItem(LEGACY_THEME_KEY); } catch (e) { /* 覚えられない環境 */ }

/** この画面が開いてから大きなタブの画面に届けられた既定(覚えられない環境でも効くように) */
let received = null;

/** 選んでいないときの明るさ。大設定の既定があればそれ、無ければライト。 */
export function shellDefault() {
  if (received) return received;
  try {
    return localStorage.getItem(SHELL_DEFAULT_KEY) === 'dark' ? 'dark' : DEFAULT;
  } catch (e) {
    return DEFAULT;
  }
}

/** 覚えている選び方(light / dark / auto)。選んでいなければ 'default'。 */
export function choice() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return CHOSEN.includes(v) ? v : DEFAULT_CHOICE;
  } catch (e) {
    return DEFAULT_CHOICE;
  }
}

/** いま実際に使う明るさ(light / dark)。 */
export function effective(c = choice()) {
  if (c === DEFAULT_CHOICE) return shellDefault();
  if (c === 'auto') return media && media.matches ? 'dark' : 'light';
  return c === 'dark' ? 'dark' : 'light';
}

/** ボタンを押したら次にどれにするか。「既定」からは**見た目が変わるほう**へ。 */
export function next(c = choice()) {
  if (c === DEFAULT_CHOICE) return effective(c) === 'dark' ? 'light' : 'dark';
  return ORDER[(ORDER.indexOf(c) + 1) % ORDER.length];
}

function titleOf(c) {
  const now = c === DEFAULT_CHOICE
    ? `既定(日報複合ツールの大設定に従う。いまは${NAME[effective(c)]})`
    : NAME[c];
  const then = next(c);
  const after = then === DEFAULT_CHOICE ? '既定(大設定に従う)' : NAME[then];
  return `画面の明るさ: ${now}。押すと${after}にします`;
}

/** `<html data-theme>` を付け替える。 */
export function apply(c = choice()) {
  const root = document.documentElement;
  root.dataset.theme = effective(c);
  root.dataset.themeChoice = c;
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.textContent = LABEL[c] || LABEL[DEFAULT_CHOICE];
    btn.title = titleOf(c);
    btn.setAttribute('aria-label', btn.title);
  }
}

/** 選び方を変えて覚える。'default' は覚えを消す(大設定の既定に従う)。 */
export function set(c) {
  try {
    if (CHOSEN.includes(c)) localStorage.setItem(THEME_KEY, c);
    else localStorage.removeItem(THEME_KEY);
  } catch (e) { /* 覚えられなくても、この画面のあいだは効く */ }
  apply(CHOSEN.includes(c) ? c : DEFAULT_CHOICE);
}

/**
 * 日報複合ツールの大きなタブの画面から「画面の色の既定」が届く(大設定で変えたとき・
 * 枠を開くたび。portal/static/js/shell.js の sendTheme)。覚えておき、**自分で選んで
 * いなければ**その色で塗り直す。
 *
 * **読み込んだ時点で受け付ける**(帯のボタンを繋ぐ `wireThemeToggle` とは切り離す)。
 * 外枠の配線(app.js)のどこかで止まっても、色の合図は取りこぼさない。
 */
let listening = false;
export function listenShell() {
  if (listening || window.parent === window) return;
  listening = true;
  window.addEventListener('message', (ev) => {
    const data = ev.data;
    if (ev.source !== window.parent) return;
    if (!data || data.type !== 'alltools:theme' || (data.theme !== 'light' && data.theme !== 'dark')) return;
    received = data.theme;
    try { localStorage.setItem(SHELL_DEFAULT_KEY, data.theme); } catch (e) { /* この画面のあいだは効く */ }
    // 選んでいればそのまま(ボタンの説明の「いまは〜」だけ変わる)
    apply();
  });
}
listenShell();

/** 帯のボタンをつなぐ。「自動」のあいだは Windows の切り替えにも付いていく。 */
export function wireThemeToggle() {
  listenShell();
  apply();
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => set(next(choice())));
  }
  if (media) {
    const follow = () => { if (choice() === 'auto') apply('auto'); };
    if (media.addEventListener) media.addEventListener('change', follow);
    else if (media.addListener) media.addListener(follow);
  }
  // 別の画面(タブ・窓)で変えたら、こちらも合わせる
  window.addEventListener('storage', (ev) => {
    if (ev.key === SHELL_DEFAULT_KEY) received = null;
    if (ev.key === THEME_KEY || ev.key === SHELL_DEFAULT_KEY) apply();
  });
}
