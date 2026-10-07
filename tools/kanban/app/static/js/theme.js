// 画面の明るさ(ライト / ダーク)
//
// 帯の右のボタンで ☀ ライト → ☾ ダーク → ◐ 自動 → ライト … と切り替える。
// **選んでいなければライト**(統合ツールの4ツールでそろえる)。
// 「自動」は Windows の設定(アプリのモード: ライト / ダーク)に合わせる。
//
// **選んだものはこの端末(この画面)に覚えさせる**(localStorage)。PC ごとの見やすさの
// 好みなので、共有フォルダの設定には入れない。覚えられない環境でも自動で動く。
//
// 実際の色は tokens.css が持つ(`:root[data-theme="dark"]` の色一式)。ここは
// `<html data-theme>` を付け替えるだけ。最初の 1 回は base.html の頭の小さな
// スクリプトが**描く前に**付ける(白く光ってから暗くなるのを防ぐ)。

export const THEME_KEY = 'kanban.theme';

const ORDER = ['light', 'dark', 'auto'];
const DEFAULT = 'light';
const LABEL = { auto: '◐ 自動', light: '☀ ライト', dark: '☾ ダーク' };
const TITLE = {
  light: '画面の明るさ: ライト。押すとダークにします',
  dark: '画面の明るさ: ダーク。押すと自動(Windows の設定に合わせる)にします',
  auto: '画面の明るさ: 自動(Windows の設定に合わせる)。押すとライトにします',
};

const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

/** 覚えている選び方(auto / light / dark)。選んでいない・読めなければライト。 */
export function choice() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return ORDER.includes(v) ? v : DEFAULT;
  } catch (e) {
    return DEFAULT;
  }
}

/** いま実際に使う明るさ(light / dark)。 */
export function effective(c = choice()) {
  if (c === 'auto') return media && media.matches ? 'dark' : 'light';
  return c;
}

/** `<html data-theme>` を付け替える。 */
export function apply(c = choice()) {
  const root = document.documentElement;
  root.dataset.theme = effective(c);
  root.dataset.themeChoice = c;
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.textContent = LABEL[c];
    btn.title = TITLE[c];
    btn.setAttribute('aria-label', TITLE[c]);
  }
}

/** 選び方を変えて覚える。 */
export function set(c) {
  // 「自動」も覚える(消すと既定のライトに戻ってしまう)
  try { localStorage.setItem(THEME_KEY, c); } catch (e) { /* 覚えられなくても、この画面のあいだは効く */ }
  apply(c);
}

/** 帯のボタンをつなぐ。「自動」のあいだは Windows の切り替えにも付いていく。 */
export function wireThemeToggle() {
  apply();
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      const next = ORDER[(ORDER.indexOf(choice()) + 1) % ORDER.length];
      set(next);
    });
  }
  if (media) {
    const follow = () => { if (choice() === 'auto') apply('auto'); };
    if (media.addEventListener) media.addEventListener('change', follow);
    else if (media.addListener) media.addListener(follow);
  }
  // 別の画面(タブ・窓)で変えたら、こちらも合わせる
  window.addEventListener('storage', (ev) => { if (ev.key === THEME_KEY) apply(); });
}
