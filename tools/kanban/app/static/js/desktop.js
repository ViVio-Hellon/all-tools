// desktop.js — デスクトップ版(Rust/Tauri)のときだけ、窓まわりを外枠に頼む
//
// ブラウザ版では今までどおりブラウザに任せる。デスクトップ版の画面は外枠の窓の
// 中にあるので、次のことは外枠(日報複合ツールの ../../src-tauri/src/services.rs)の仕事になる:
//
//   ・帳票を別の窓で開く(`target="_blank"` のリンク)        … open_window
//   ・社内サイトなど、アプリの外のページを既定のブラウザで開く … open_external
//   ・CSV・なぜなぜシートを「名前を付けて保存」で保存する    … save_file
//   ・設定の「参照...」で OS の標準のフォルダ/ファイル選択   … pick_path
//   ・モードを変えたあと、Python だけを立て直す(窓は閉じない) … restart_app
//
// どちらで動いているかは `window.__TAURI__` があるかで分かる。

const tauri = window.__TAURI__;

/** デスクトップ版で動いているか。 */
export const isDesktop = Boolean(tauri && tauri.core && tauri.core.invoke);

function invoke(cmd, args, options) {
  return tauri.core.invoke(cmd, args, options);
}

/**
 * アプリの中のページ(帳票など)を別の窓で開く。
 * `url` は `/report/site?line=L1&t=…` のようなアプリの中の経路。
 */
export function openWindow(url, title = '') {
  if (isDesktop) {
    invoke('open_window', { url, title: title || null })
      .catch((err) => console.error('窓を開けませんでした', err));
    return true;
  }
  return Boolean(window.open(url, '_blank'));
}

/** アプリの外のページを開く(デスクトップ版は既定のブラウザで)。 */
export function openExternal(url) {
  if (isDesktop) {
    invoke('open_external', { url }).catch((err) => console.error('開けませんでした', err));
    return;
  }
  window.open(url, '_blank', 'noopener');
}

// ------------------------------------------------------------------
// 保存(ダウンロードの代わり)
// ------------------------------------------------------------------
let saveDir = '';

/** 保存ダイアログを最初に開くフォルダ(設定の「CSV の出力先」)。 */
export function setSaveDir(dir) {
  saveDir = dir || '';
}

/**
 * 中身を「名前を付けて保存」で保存する。保存した場所(やめたら null)を返す。
 * `data` は Blob / ArrayBuffer / Uint8Array。
 */
export async function saveFile(name, data) {
  const bytes = data instanceof Blob ? new Uint8Array(await data.arrayBuffer())
    : data instanceof Uint8Array ? data : new Uint8Array(data);
  return invoke('save_file', bytes, {
    headers: {
      // 見出しは ASCII しか通らないので、日本語のファイル名は %XX にして送る
      'x-file-name': encodeURIComponent(name),
      'x-save-dir': encodeURIComponent(saveDir),
    },
  });
}

/** `Content-Disposition` からファイル名を拾う(`filename*=UTF-8''…` を優先)。 */
function nameFrom(disposition, fallback) {
  if (!disposition) return fallback;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  if (star) {
    try { return decodeURIComponent(star[1]); } catch (e) { /* そのまま下へ */ }
  }
  const plain = /filename="?([^";]+)"?/i.exec(disposition);
  return plain ? plain[1] : fallback;
}

/**
 * `<a download>` を押したら(画面が `a.click()` で押した場合も)、外枠の保存
 * ダイアログで保存する。中身は画面から取りに行く(アプリの中の経路・blob: のどちらも)。
 */
function watchDownloads() {
  document.addEventListener('click', async (event) => {
    const link = event.target.closest && event.target.closest('a[download]');
    if (!link || !link.href || event.defaultPrevented) return;
    event.preventDefault();
    try {
      const res = await fetch(link.href, {
        headers: window.APP && window.APP.token ? { 'X-Tool-Token': window.APP.token } : {},
      });
      if (!res.ok) {
        let message = `保存する中身を取れませんでした(${res.status})`;
        try {
          const body = await res.json();
          if (body && body.error && body.error.message) message = body.error.message;
        } catch (e) { /* JSON でなければ番号だけ */ }
        throw new Error(message);
      }
      const fallback = link.getAttribute('download')
        || decodeURIComponent(new URL(link.href, location.href).pathname.split('/').pop() || 'download');
      const name = nameFrom(res.headers.get('Content-Disposition'), fallback);
      const saved = await saveFile(name, await res.blob());
      if (saved) notify('ok', `保存しました: ${saved}`);
    } catch (err) {
      notify('bad', `保存できませんでした: ${err && err.message ? err.message : err}`);
    }
  });
}

/** 画面のお知らせ(toast.js)を、読み込みの順番に縛られずに出す */
function notify(kind, message) {
  import('./toast.js').then((t) => (t[kind] || t.ok)(message)).catch(() => alert(message));
}

// ------------------------------------------------------------------
// 選ぶ(設定の「参照...」)
// ------------------------------------------------------------------
/**
 * OS の標準のダイアログでフォルダ/ファイルを選ぶ。選んだパス(やめたら null)。
 * `kind` は 'folder' か 'file'。`extensions` は ['sqlite3', 'accdb'] のように。
 */
export function pickPath(kind, { start = '', title = '', extensions = [] } = {}) {
  return invoke('pick_path', { kind, start: start || null, title: title || null, extensions });
}

// ------------------------------------------------------------------
// 立て直し(モードの切り替え)
// ------------------------------------------------------------------
/** Python だけを立て直す。前の Python は最後の書き戻しを済ませてから終わる。 */
export function restartApp() {
  return invoke('restart_app');
}

/**
 * `target="_blank"` のリンクを、デスクトップ版では外枠に開いてもらう
 * (外枠の窓は新しいタブを持たないので、そのままでは何も起きない)。
 */
function watchLinks() {
  document.addEventListener('click', (event) => {
    const link = event.target.closest && event.target.closest('a[target="_blank"]');
    if (!link || !link.href || link.hasAttribute('download')) return;
    event.preventDefault();
    const url = new URL(link.href, location.href);
    if (url.origin === location.origin) openWindow(url.pathname + url.search, link.dataset.title || link.textContent.trim());
    else openExternal(url.href);
  });
}

/** デスクトップ版のときだけ、窓まわりの見張りを立てる(app.js が最初に呼ぶ)。 */
export function install() {
  if (!isDesktop) return;
  document.documentElement.classList.add('is-desktop');
  watchLinks();
  watchDownloads();
}
