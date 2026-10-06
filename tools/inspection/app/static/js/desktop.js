/*
  desktop.js — デスクトップ版(Tauri)のときだけ、窓まわりを外枠(Rust)に頼む

  ブラウザ版では何もしない(ボタンも出さない)。どちらで動いているかは
  `window.__TAURI__` があるかで分かる(python-web-tools の desktop.js と同じ)。

    pickFolder   … Windows の「フォルダーの選択」窓(統合ツールの外枠 services.rs の pick_folder)
    openExternal … アプリの外のページを既定のブラウザで開く
*/

const tauri = window.__TAURI__;

/** デスクトップ版で動いているか。 */
export const isDesktop = Boolean(tauri && tauri.core && tauri.core.invoke);

/**
 * フォルダーの選択窓を出す。選んだフォルダ(選ばなければ null)。
 * ブラウザ版では使えない(呼ぶ側がボタンを出さない)。
 */
export async function pickFolder(current = "", title = "") {
  if (!isDesktop) return null;
  return tauri.core.invoke("pick_folder", { current: current || null, title: title || null });
}

/** アプリの外のページを開く。 */
export function openExternal(url) {
  if (isDesktop) {
    tauri.core.invoke("open_external", { url }).catch((err) => console.error("開けませんでした", err));
    return;
  }
  window.open(url, "_blank", "noopener");
}

/** デスクトップ版のときだけ出す部品(`data-desktop-only`)を見せる。 */
export function revealDesktopOnly(scope = document) {
  if (!isDesktop) return;
  for (const node of scope.querySelectorAll("[data-desktop-only]")) node.hidden = false;
  document.documentElement.dataset.edition = "desktop";
}
