/*
  desktop.js — デスクトップ版(日報複合ツールの窓)のときだけ、窓まわりを外枠に頼む

  ブラウザ版では今までどおりブラウザに任せる。デスクトップ版の画面は外枠
  (Rust/Tauri)の窓の中にあり、新しいタブもアドレス欄も無いので、次のことは
  外枠の仕事になる:

    ・紙(日報・GW)や VC 早見表を別の窓で開く        … open_window
    ・参照パスの「フォルダを選ぶ…」で OS の標準のダイアログ … pick_path
    ・社内サイトなど、アプリの外のページを既定のブラウザで … open_external

  どちらで動いているかは `window.__TAURI__` があるかで分かる(看板・
  カレンダー・点検表・python-web-tools の desktop.js と同じ)。日報複合ツールの
  大きなタブの中では、外枠が差し込む台本(`embed.js`)がこれを用意し、頼みごとを
  大きなタブの画面経由で外枠へ渡す。ここはその違いを知らなくてよい。
*/

const tauri = window.__TAURI__;

/** デスクトップ版で動いているか。 */
export const isDesktop = Boolean(tauri && tauri.core && tauri.core.invoke);

function invoke(cmd, args) {
  return tauri.core.invoke(cmd, args);
}

/**
 * アプリの中のページ(紙・早見表)を別の窓で開く。開けたら(頼めたら)true。
 * ブラウザ版では何もせず false を返す ── 呼ぶ側が今までどおり `window.open` する。
 *
 * `label` を付けると、同じ名前の窓が開いていればそれを前に出す(VC 早見表)。
 */
export function openWindow(url, { title = "", label = "", width = 0, height = 0 } = {}) {
  if (!isDesktop) return false;
  invoke("open_window", {
    url, title: title || null, label: label || null,
    width: width || null, height: height || null,
  }).catch((err) => console.error("窓を開けませんでした", err));
  return true;
}

/** 紙などを開く。デスクトップ版は別の窓、ブラウザ版は新しいタブ。 */
export function openPage(url, title = "") {
  if (!openWindow(url, { title })) window.open(url, "_blank", "noopener");
}

/** アプリの外のページを開く(デスクトップ版は既定のブラウザで)。 */
export function openExternal(url) {
  if (isDesktop) {
    invoke("open_external", { url }).catch((err) => console.error("開けませんでした", err));
    return;
  }
  window.open(url, "_blank", "noopener");
}

/**
 * OS の標準のダイアログでフォルダ/ファイルを選ぶ。選んだパス(やめたら null)。
 * `kind` は 'folder' か 'file'。共有フォルダ(`\\サーバ\…`)も辿れる。
 */
export function pickPath(kind, { start = "", title = "", extensions = [] } = {}) {
  return invoke("pick_path", { kind, start: start || null, title: title || null, extensions });
}

/**
 * `target="_blank"` のリンクを、デスクトップ版では外枠に開いてもらう
 * (窓は新しいタブを持たないので、そのままでは何も起きない)。
 */
function watchLinks() {
  document.addEventListener("click", (event) => {
    const link = event.target.closest && event.target.closest('a[target="_blank"]');
    if (!link || !link.href || link.hasAttribute("download") || event.defaultPrevented) return;
    event.preventDefault();
    const url = new URL(link.href, location.href);
    if (url.origin === location.origin) {
      openWindow(url.pathname + url.search, { title: link.dataset.title || link.textContent.trim() });
    } else {
      openExternal(url.href);
    }
  });
}

/** デスクトップ版のときだけ、窓まわりの見張りを立てる(app.js が最初に呼ぶ)。 */
export function install() {
  if (!isDesktop) return;
  document.documentElement.classList.add("is-desktop");
  document.documentElement.dataset.edition = "desktop";
  watchLinks();
}
