/*
  hint.js — マウスを乗せると簡易説明 (v4.7.0)

      マウスムーブで簡易説明が出るようにしてください

  `data-hint="…"` の付いた要素にマウスを乗せて少し待つと、その下に短い
  説明が出ます。**字はサーバが付けます**(日報入力の欄は
  `nippou/logic/input_shortcuts.HINTS`)── ここは出して消すだけです。

  【`title` を使わない理由】
  ブラウザの `title` は出るまで1秒ほどかかり、字も小さく、色も画面の
  明るさ(ライト/ダーク)に合いません。ここは 0.4 秒で、画面と同じ色で
  出します。

  【邪魔をしない】
  ・押した・打った・画面が動いた、で消えます(打っている欄の上に残らない)
  ・マウスのときだけ出します(タッチで触ると、押す前に説明が被さるので)
  ・**画面を差し替えても1つだけ。** document に1度だけ付けるので、
    画面ごとに付け直しません(`app.js` から1回呼ぶ)
*/

// 乗せてから出るまで(ms)。表の上を通り過ぎるだけでは出ないように
const DELAY_MS = 400;

let bubble = null;
let current = null;
let timer = null;

function ensureBubble() {
  if (bubble?.isConnected) return bubble;
  bubble = document.createElement("div");
  bubble.className = "hint";
  bubble.id = "hint-bubble";
  bubble.setAttribute("role", "tooltip");
  bubble.hidden = true;
  document.body.appendChild(bubble);
  return bubble;
}

function place(box, el) {
  const at = el.getBoundingClientRect();
  const gap = 6;
  const width = box.offsetWidth;
  const height = box.offsetHeight;
  // 下に入らなければ上へ。左右ははみ出さないように寄せる
  const below = at.bottom + gap;
  const top = below + height + 8 <= window.innerHeight
    ? below : Math.max(8, at.top - height - gap);
  const left = Math.max(8, Math.min(at.left, window.innerWidth - width - 8));
  box.style.top = `${Math.round(top)}px`;
  box.style.left = `${Math.round(left)}px`;
}

function show(el) {
  if (!el.isConnected || current !== el) return;
  const text = (el.dataset.hint || "").trim();
  if (!text) return;
  const box = ensureBubble();
  box.textContent = text;
  box.hidden = false;
  place(box, el);
}

/** 説明を消す。乗せかけの待ちも捨てる。 */
export function hideHint() {
  clearTimeout(timer);
  timer = null;
  current = null;
  if (bubble) bubble.hidden = true;
}

export function startHints() {
  document.addEventListener("pointerover", (event) => {
    if (event.pointerType && event.pointerType !== "mouse") return;
    const el = event.target.closest?.("[data-hint]");
    if (el === current) return;
    hideHint();
    if (!el || !(el.dataset.hint || "").trim()) return;
    current = el;
    timer = setTimeout(() => show(el), DELAY_MS);
  });
  document.addEventListener("pointerout", (event) => {
    if (!current) return;
    const to = event.relatedTarget;
    if (to && current.contains(to)) return;      // 中の字へ移っただけ
    hideHint();
  });
  // 押した・打った・動いた ── どれも説明を読んでいる場面ではない
  document.addEventListener("pointerdown", hideHint, true);
  document.addEventListener("keydown", hideHint, true);
  document.addEventListener("scroll", hideHint, true);
  window.addEventListener("blur", hideHint);
}
