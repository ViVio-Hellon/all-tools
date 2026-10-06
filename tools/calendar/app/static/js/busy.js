/*
  busy.js — 押したボタンに「待っている」ことを出す

  **押した本人にだけ出す。** 画面全体を覆うと、待っていないところまで
  止まって見える。250ms より速く返るものには何も出さない ── 一瞬で
  終わるものに待機の姿を出すと、画面がちらつくだけになる。
*/

const DELAY_MS = 250;

let lastPressed = null;

/** いま押されたボタンを覚える。`api.js` がこれを読む。 */
export function watchClicks() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    lastPressed = button && !button.disabled ? button : null;
  }, true);
}

export function pressed() {
  return lastPressed;
}

/**
 * 待機の姿を出す。戻り値を呼ぶと消える。
 * @param {HTMLElement|null} el
 * @returns {() => void}
 */
export function mark(el) {
  if (!el) return () => {};
  let shown = false;
  const timer = setTimeout(() => {
    shown = true;
    el.dataset.busy = "1";
    el.disabled = true;
  }, DELAY_MS);

  return () => {
    clearTimeout(timer);
    if (!shown) return;
    delete el.dataset.busy;
    el.disabled = false;
  };
}
