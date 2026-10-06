/* toast.js — 画面の右下のお知らせ(数秒で消える) */
export function toast(message, kind = "ok", ms = 4200) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const node = document.createElement("div");
  node.className = `toast toast--${kind}`;
  node.textContent = message;
  host.append(node);
  setTimeout(() => node.remove(), ms);
}

export function toastError(err) {
  toast((err && err.message) || String(err), "ng", 7000);
}
