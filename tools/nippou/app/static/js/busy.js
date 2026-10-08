/*
  busy.js — 押されたボタンに「待っている」ことを見せる

  どの画面も「押す → サーバへ投げる → 返ってきた画面を描く」で
  出来ているので、投げるところ(api.js)を1か所だけ捕まえれば、
  画面を1つずつ書き換えなくても全部に効く。
*/

// いま押されたボタン。`api.js` が送信を始めるときに読む
let pressed = null;
let forget = null;

/*
  **押したボタンを覚えておくのは、押した直後だけ**(v4.24.0)。

  前は「最後に押したボタン」をずっと覚えていたので、あとから走った**関係の
  ない通信**(欄から離れたときの「決まる値」・自動保存)まで、そのボタンの
  待ちとして扱っていました。250ms を超えるとボタンが無効になり、その間に
  押しても効きません(「この直をチェック」を押したのに何も起きない)。

  押したその処理の中で始まった通信だけを、そのボタンの待ちにします:
  クリックの処理(確かめの窓を含む)が終わったあとの少しのあいだだけ覚え、
  キーを打つ・ほかの欄へ移ると忘れます。
*/
const PRESS_MS = 50;

/** 直前に押されたボタン。 */
export function current() {
  return pressed;
}

function drop() {
  pressed = null;
  clearTimeout(forget);
}

/**
 * クリックを見張る。**捕捉フェーズで拾う** ── 画面側の listener が
 * `stopPropagation()` していても、押されたことは分かるようにする。
 */
export function watchClicks() {
  document.addEventListener("click", (event) => {
    const btn = event.target.closest("button, .btn");
    drop();
    pressed = btn || null;
    // タイマーはクリックの処理が終わってから動く(`confirm` で止まっている
    // あいだは進まない)── 押した処理の中で始めた通信は、必ずこのボタンの待ち
    if (pressed) forget = setTimeout(drop, PRESS_MS);
  }, true);
  // 打つ・ほかの欄へ移るのは、もう別の操作
  document.addEventListener("keydown", drop, true);
  document.addEventListener("focusin", (event) => {
    if (pressed && event.target !== pressed) drop();
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
