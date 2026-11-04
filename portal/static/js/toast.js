/* toast.js — 画面の右下のお知らせ(数秒で消える。`ms` が 0 なら押すまで残す) */
export function toast(message, kind = "ok", ms = 4200) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const node = document.createElement("div");
  node.className = `toast toast--${kind}`;
  node.textContent = message;
  host.append(node);
  if (ms > 0) {
    setTimeout(() => node.remove(), ms);
  } else {
    // 0: 読んでもらうまで残す(管理者の停止で強制で止めたときの知らせなど)。押すと閉じる
    node.title = "押すと閉じます";
    node.classList.add("toast--stay");
    node.addEventListener("click", () => node.remove());
  }
}

export function toastError(err) {
  toast((err && err.message) || String(err), "ng", 7000);
}
