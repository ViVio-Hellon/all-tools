/*
  api.js — 入口の Python へ問い合わせる(起動トークンを付ける)

  断られたときは ApiError を投げる。文言はサーバが持つ(`error.message`)。
*/
export class ApiError extends Error {
  constructor(status, code, message, body) {
    super(message || `問い合わせに失敗しました(${status})`);
    this.status = status;
    this.code = code || "";
    this.body = body || {};
  }
}

async function call(method, path, payload) {
  const headers = { "X-Tool-Token": (window.SHELL && window.SHELL.token) || "" };
  const init = { method, headers, cache: "no-store" };
  if (payload !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(payload);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    throw new ApiError(0, "offline", "統合ツールの処理に届きません。開き直してください。");
  }
  let body = {};
  try { body = await res.json(); } catch (err) { /* JSON でなければ空 */ }
  if (!res.ok) {
    const e = body.error || {};
    throw new ApiError(res.status, e.code || body.reason, e.message || body.message, body);
  }
  return body;
}

export const api = {
  get: (path) => call("GET", path),
  post: (path, payload = {}) => call("POST", path, payload),
};
