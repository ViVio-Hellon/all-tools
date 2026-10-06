/*
  busy.js — 押されたボタンに「待っている」ことを見せる

  どの画面も「押す → サーバへ投げる → 返ってきた画面を描く」で
  出来ているので、投げるところ(api.js)を1か所だけ捕まえれば、
  画面を1つずつ書き換えなくても全部に効く。
*/

// いま押されたボタン。`api.js` が送信を始めるときに読む
let pressed = null;

/** 直前に押されたボタン。 */
export function current() {
  return pressed;
}

/**
 * クリックを見張る。**捕捉フェーズで拾う** ── 画面側の listener が
 * `stopPropagation()` していても、押されたことは分かるようにする。
 */
export function watchClicks() {
  document.addEventListener("click", (event) => {
    const btn = event.target.closest("button, .btn");
    pressed = btn || null;
  }, true);
}

/**
 * 待機の姿にする。返り値を呼ぶと元に戻る。
 *
 * 250ms より速く返るものには何も出さない ── 一瞬で終わるものに
 * 待機の姿を出すと、画面がちらつくだけになる。
 */
export function mark(node) {
  if (!node) return () => {};
  let shown = false;
  const timer = setTimeout(() => {
    shown = true;
    node.dataset.busy = "1";
    node.setAttribute("aria-busy", "true");
    if ("disabled" in node) node.disabled = true;
  }, 250);

  return () => {
    clearTimeout(timer);
    if (!shown) return;
    delete node.dataset.busy;
    node.removeAttribute("aria-busy");
    if ("disabled" in node) node.disabled = false;
  };
}

// api.js から呼びやすい別名
export { current as pressed };
