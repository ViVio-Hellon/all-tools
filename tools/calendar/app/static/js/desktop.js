/*
  desktop.js — デスクトップ版(Tauri)のときだけ、窓まわりを外枠(Rust)に頼む

  ブラウザ版では今までどおり `window.open` などを使う。デスクトップ版の画面は
  外枠の窓の中にあるので、別の窓を開く・閉じる・ファイルに保存する は外枠の仕事に
  なる(統合ツールの外枠 `../../src-tauri/src/services.rs` の `open_window` ほか)。
  どちらで動いているかは `window.__TAURI__` があるかで分かる。
  作りは梱包資材総合ツール(python-web-tools)の desktop.js と同じ。
*/

const tauri = window.__TAURI__;

/** デスクトップ版で動いているか。 */
export const isDesktop = Boolean(tauri && tauri.core && tauri.core.invoke);

/**
 * アプリの中のページ(印刷など)を別の窓で開く。開けたら true。
 * `url` は `/print?year=…&t=…` のようなアプリの中の経路。
 */
export function openWindow(url, title = "") {
  if (isDesktop) {
    tauri.core.invoke("open_window", { url, title: title || null })
      .catch((err) => console.error("窓を開けませんでした", err));
    return true;
  }
  return Boolean(window.open(url, "_blank"));
}

/**
 * 文字をファイルに保存する。デスクトップ版は**保存先を訊く窓**(OS のもの)を出す。
 * ブラウザ版はダウンロードにする。保存したら true、やめたら false。
 */
export async function saveText(fileName, text) {
  if (isDesktop) {
    return Boolean(await tauri.core.invoke("save_text_file", { fileName, text }));
  }
  const blob = new Blob(["﻿" + text.replace(/\n/g, "\r\n")],
                        { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  return true;
}
