// サーバとのやりとり。**ここ以外で fetch を書かない**
//
// 起動トークンの付与、エラーの形の解釈、接続断の知らせをここへ集める。
// 画面ごとに書くと、トークンの付け忘れや、断りの種類の取り違えが起きる。

const TOKEN = window.APP.token;

/**
 * この画面の名札 (`kanban/screen.py`)
 *
 * **プロセスが1つでも、タブは何枚でも繋がる。** 同じアプリを2枚開くと
 * どちらでも押せてしまい、同じ看板を2画面で取り合うことになる。サーバは
 * 名札で画面を見分け、持ち主でない画面からの操作を断る。
 *
 * **読み込みのたびに作り直す。** `sessionStorage` に覚えると、タブを複製
 * したときに名札まで複製されて同じ画面が2枚になる。再読込は `pagehide` で
 * 「閉じた」と伝わるので、新しい名札がそのまま持ち主を引き継ぐ。
 */
export const SCREEN =
  (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : `s-${Date.now()}-${Math.random().toString(36).slice(2)}`;

// 接続が切れたことを外枠へ知らせる(health.js が帯を出す)
const listeners = new Set();

export function onConnectionChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function announce(ok) {
  listeners.forEach((fn) => {
    try { fn(ok); } catch (e) { console.error(e); }
  });
}

/** サーバが返した断り。`code` で種類を見る(文言から推し量らない) */
export class ApiError extends Error {
  constructor(status, body) {
    const info = (body && body.error) || {};
    // **想定外のエラー(5xx)には記録番号を添える。** 設定 →「記録」でその番号を開けば、
    // 入力・原因の連鎖・直前の操作まで見られる(kanban/trace.py)。4xx(断り)は文だけ出す
    // (番号は e.ref に持つ)
    const ref = info.ref || '';
    const base = info.message || `通信に失敗しました (${status})`;
    super(status >= 500 && ref && !base.includes(ref) ? `${base}(記録番号 ${ref})` : base);
    this.ref = ref;
    this.name = 'ApiError';
    this.status = status;
    this.code = info.code || 'unknown';
    this.field = info.field || '';
    // 断られたときでも、サーバは更新後の盤面を一緒に返してくれる。
    // 画面はこれで描き直せば、押す前の状態に戻らずに済む
    this.body = body || {};
  }
}

async function request(path, options = {}) {
  const opts = {
    method: options.method || 'GET',
    headers: { 'X-Tool-Token': TOKEN, 'X-Screen': SCREEN },
    cache: 'no-store',
  };
  if (options.bytes !== undefined) {
    // ファイルの中身をそのままのバイト列で(`api.postBytes`)
    opts.headers['Content-Type'] = 'application/octet-stream';
    Object.assign(opts.headers, options.headers || {});
    opts.body = options.bytes;
  } else if (options.form !== undefined) {
    // ファイルを送る(multipart)。Content-Type はブラウザが境界付きで付ける
    opts.body = options.form;
  } else if (options.body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(options.body);
  }

  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    // ネットワークまで届かなかった = バックエンドが落ちている
    announce(false);
    throw new ApiError(0, { error: { code: 'offline', message: 'バックエンドに接続できません' } });
  }
  announce(true);

  let body = null;
  const type = res.headers.get('Content-Type') || '';
  if (type.includes('application/json')) {
    try { body = await res.json(); } catch (e) { body = null; }
  }
  if (res.status === 423) {
    // 持ち主でない画面からの操作。**心拍を待たずに覆いを出す** ── 次の
    // 心拍まで数秒あり、そのあいだ「押しても断られる画面」になってしまう
    window.dispatchEvent(new CustomEvent('screen-blocked'));
  }
  if (!res.ok) throw new ApiError(res.status, body);
  return body;
}

export const api = {
  get: (path) => request(path),
  post: (path, body) => request(path, { method: 'POST', body: body || {} }),
  /** ファイルを送る(`FormData`)。起動トークンと画面の名札は同じように付く */
  upload: (path, form) => request(path, { method: 'POST', form }),
  /**
   * ファイルの中身を**そのままのバイト列**で送る(名前と大きさは `path` の問い合わせに付ける)。
   * デスクトップ版の窓(WebView2)は、FormData に入れたファイルの中身をアプリの中の通り道へ
   * 渡さないことがあり、空のファイルが届いていた。メモリに読み込んだバイト列ならそのまま届く。
   * `creds`(管理者パスワード)は見出しで送る(日本語も通るよう encodeURIComponent する)
   */
  postBytes: (path, bytes, creds = {}) => request(path, {
    method: 'POST', bytes,
    headers: {
      'X-Admin-Password': encodeURIComponent(creds.password || ''),
      'X-Admin-Password-Confirm': encodeURIComponent(creds.password_confirm || ''),
    },
  }),
};

/** 一覧などの URL に line を足す小道具 */
export function withLine(path, line) {
  if (!line) return path;
  return `${path}${path.includes('?') ? '&' : '?'}line=${encodeURIComponent(line)}`;
}
