/*
  busy.js — 押したボタンに「待っている」ことを出す

  **押した本人にだけ出す。** 画面全体を覆うと、待っていないところまで
  止まって見える。250ms より速く返るものには何も出さない ── 一瞬で
  終わるものに待機の姿を出すと、画面がちらつくだけになる。

  【押したボタンは、その押下の中でだけ覚える】
  以前は最後に押したボタンを**いつまでも**覚えていた。帯の同期の
  問い合わせ(数秒ごと・裏で勝手に走る)も `api.js` を通るので、それが
  250ms を超えると**前に押した「翌月」が待機の姿になって押せなくなり**、
  そのあいだに押した1回が黙って消えた。押してもいないボタンを
  止めない ── 次の tick で忘れる(点検表の `busy.js` と同じ)。

  【`disabled` には触らない】
  以前は待機中に `disabled = true` にし、終わったら `false` に戻していた。
  そのあいだに画面が描き直されて「これ以上先の月は無い」で押せなく
  なっていても、終わったときに押せる形へ戻してしまう。押せるかどうかは
  サーバの返事を描いた画面が決めるもので、ここは**待機の印だけ**を持つ
  (`data-busy`。印のあいだは押しても届かない ── 二度押しを防ぐ)。
*/

const DELAY_MS = 250;

let lastPressed = null;

/** いま押されたボタンを覚える。`api.js` がこれを読む。 */
export function watchClicks() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    // 待機中のものは押しても届かせない(同じ処理を2回走らせない)
    if (button && button.dataset.busy === "1") {
      event.preventDefault();
      event.stopImmediatePropagation();
      lastPressed = null;
      return;
    }
    lastPressed = button && !button.disabled ? button : null;
    // 同じ押下の中でしか使わない。次の tick で捨てる
    setTimeout(() => { lastPressed = null; }, 0);
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
    el.setAttribute("aria-busy", "true");
  }, DELAY_MS);

  return () => {
    clearTimeout(timer);
    if (!shown) return;
    delete el.dataset.busy;
    el.removeAttribute("aria-busy");
  };
}
