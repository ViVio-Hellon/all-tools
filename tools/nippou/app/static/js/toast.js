/*
  toast.js — 短い知らせ

  **文言はサーバが持つ。** ここは受け取ったものを出すだけで、
  「保存しました」のような業務の言葉を組み立てない(設計 §1)。
*/

const HOLD_MS = { ok: 3200, info: 4000, warn: 6000, error: 9000 };

function box() {
  return document.getElementById("toasts");
}

/**
 * 知らせを1つ出す。
 * @param {string} text 出す文言(サーバが決めたもの)
 * @param {"ok"|"info"|"warn"|"error"} kind
 */
export function toast(text, kind = "info") {
  const host = box();
  if (!host) return;

  const node = document.createElement("div");
  node.className = `toast toast--${kind}`;
  // 重い知らせは読み上げにも割り込ませる
  node.setAttribute("role", kind === "error" ? "alert" : "status");

  const body = document.createElement("div");
  body.className = "toast__text";
  body.textContent = text;

  const close = document.createElement("button");
  close.className = "toast__close";
  close.type = "button";
  close.setAttribute("aria-label", "閉じる");
  close.textContent = "×";
  close.addEventListener("click", () => node.remove());

  node.append(body, close);
  host.appendChild(node);

  setTimeout(() => node.remove(), HOLD_MS[kind] ?? HOLD_MS.info);
  return node;
}

/**
 * サーバが断った理由をそのまま出す。
 *
 * `ApiError` は文言をサーバの応答から取っている(api.js)。ここで
 * 言い換えると、**通信は成功しているのに通信の失敗として案内される**
 * ような食い違いが起きる。
 */
export function toastError(err) {
  toast(err?.message || "処理に失敗しました", "error");
}
