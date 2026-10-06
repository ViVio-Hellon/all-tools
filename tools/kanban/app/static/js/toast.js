// 短い知らせ。**押した結果が見えないことをなくす**ためのもの
//
// tkinter 版は何をしても MessageBox が出ていた。毎回「OK」を押させるのは
// 手数が増えるだけなので、うまくいったことは流れて消える知らせにする。
// 断られたときだけは消さずに残す(理由を読む時間が要る)。

const HOLD_MS = { ok: 2200, info: 2600, warn: 5000, bad: 7000 };

export function toast(message, kind = 'info') {
  const box = document.getElementById('toasts');
  if (!box) return;

  const el = document.createElement('div');
  el.className = `toast toast--${kind}`;
  el.textContent = message;
  box.appendChild(el);

  const life = HOLD_MS[kind] || HOLD_MS.info;
  const timer = setTimeout(() => el.remove(), life);
  // 押せば消せる。読み終わったものを待たされない
  el.addEventListener('click', () => { clearTimeout(timer); el.remove(); });
}

export const ok = (m) => toast(m, 'ok');
export const warn = (m) => toast(m, 'warn');
export const bad = (m) => toast(m, 'bad');
