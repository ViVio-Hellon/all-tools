/*
  api.js — サーバとのやりとり(python-web-tools の api.js と同じ作法)

  JSが持つのは「呼ぶ」「受け取ったものを描く」だけ。
  **業務の判断はサーバが行う**(文言もサーバが持つ)。
*/

import * as busy from "./busy.js";
import * as clientlog from "./clientlog.js";

const TOKEN = window.APP.token;

/** 自動終了の見張りが「どの画面からか」を知るための見出し。 */
export const SCREEN_HEADER = "X-Screen-Id";

/** このタブの番号(サーバが画面を組み立てるときに振った)。 */
export function screenId() {
  return (window.APP && window.APP.screenId) || "";
}

/**
 * サーバが返したエラーを、そのまま画面に出せる形で持つ。
 * 形は `{"error": {"code", "message", "detail"}}`(app/errors.py)。
 */
export class ApiError extends Error {
  constructor(status, body) {
    const info = (body && body.error) || {};
    super(info.message || (body && body.message)
          || `通信に失敗しました (HTTP ${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = info.code || "";
    this.detail = info.detail || "";
    // 問い合わせ番号(サーバがこの要求に振った番号。ログから引ける)
    this.ref = info.ref || "";
    this.body = body;
  }
}

async function request(path, options = {}) {
  // 押した本人に、いま待っていることを伝える(busy.js)
  const done = busy.mark(busy.pressed());
  try {
    let res;
    try {
      res = await fetch(path, {
        ...options,
        cache: "no-store",
        headers: {
          "X-Tool-Token": TOKEN,
          [SCREEN_HEADER]: screenId(),
          ...(typeof options.body === "string" ? { "Content-Type": "application/json" } : {}),
          ...(options.headers || {}),
        },
      });
    } catch (err) {
      const e = new ApiError(0, { error: { code: "OFFLINE",
        message: "サーバーに接続できません。しばらく待ってからもう一度お試しください。" } });
      e.cause = err;
      clientlog.noteOffline(path);          // サーバに届かないので、つながったら残す
      throw e;
    }
    let body = null;
    const type = res.headers.get("Content-Type") || "";
    if (type.includes("application/json")) body = await res.json();
    if (!res.ok) {
      const e = new ApiError(res.status, body);
      e.ref = e.ref || res.headers.get("X-Request-Ref") || "";
      throw e;
    }
    return body;
  } finally {
    done();
  }
}

/*
  `<img src>` のように**ヘッダを付けられない**読み込みのためのURL。
  起動トークンはクエリでも受け付ける(`app/__init__.py`)。
*/
export function tokenUrl(path) {
  return `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(TOKEN)}`;
}

export const api = {
  get: (path) => request(path),
  post: (path, data) => request(path, { method: "POST", body: JSON.stringify(data ?? {}) }),
};
