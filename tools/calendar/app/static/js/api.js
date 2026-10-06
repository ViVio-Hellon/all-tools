/*
  api.js — サーバとのやりとり

  JSが持つのは「呼ぶ」「受け取ったものを描く」だけ。
  **業務の判断はサーバが行う**(押せるか・隠すか・何と出すか)。
  詳細は docs/設計.md §1。
*/

import * as busy from "./busy.js";
import * as screen from "./screen.js";

const TOKEN = window.APP.token;

/**
 * サーバが返した断りを、そのまま画面に出せる形で持つ。
 *
 * 断りの返し方は2通りある:
 *
 *   入力の形の誤り   … 400 `{"error": {"code", "message"}}`  状態は動いていない
 *   業務としての断り … 422/409 同じ形                        状態は動いていない
 *
 * `code` で見分ける。**文言からは推し量らない** ── 文言を直した日に
 * 区別が壊れる(docs/設計.md §1 の規則4)。
 */
export class ApiError extends Error {
  constructor(status, body) {
    const info = (body && body.error) || {};
    super(info.message || (body && body.message)
          || `通信に失敗しました (HTTP ${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = info.code || "";
    this.hint = info.hint || "";
    this.body = body;
  }
}

async function request(path, options = {}) {
  /*
    **押した本人に、いま待っていることを伝える。**

    どの画面も「押す → サーバへ投げる → 返ってきた画面を描く」で
    出来ているので、投げるところを1か所だけ捕まえれば、画面を
    1つずつ書き換えなくても全部に効く。伝える相手は
    `busy.watchClicks()` が覚えている**いま押されたボタン**。
  */
  const done = busy.mark(busy.pressed());
  try {
    const res = await fetch(path, {
      ...options,
      cache: "no-store",
      headers: {
        "X-Tool-Token": TOKEN,
        // どの画面からの要求か。**2枚目のタブからは書かせない**
        // (`calendar_app/screen_lock.py`)
        "X-Screen-Id": screen.id(),
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });

    let body = null;
    const type = res.headers.get("Content-Type") || "";
    if (type.includes("application/json")) body = await res.json();
    if (!res.ok) {
      if (staleToken(res.status, body)) {
        // **読み込み直せば直るので、エラーを見せずにやり直す。**
        // 夜のあいだに自動終了して朝また起動すると、開けっぱなしのタブが
        // 持っている起動トークンは前のプロセスのもので、押すと全部断られる。
        // 見張り(`health.js`)も気づくが、そちらは15秒ごとなので、
        // その隙に押した人には「通信に失敗しました」としか出なかった
        location.reload();
        // 読み込み直すまでのあいだ、呼んだ側に嘘の成功を返さない
        await new Promise(() => {});
      }
      throw new ApiError(res.status, body);
    }
    return body;
  } finally {
    done();
  }
}

/** 起動トークンが古い(= サーバが入れ替わった)ことによる断りか。 */
function staleToken(status, body) {
  // **`bad_host` / `bad_origin` では読み込み直さない。** あちらは
  // 読み込み直しても直らないので、繰り返すだけになる
  return status === 403 && !!body && !!body.error
    && body.error.code === "bad_token";
}

/*
  `<a href>` や新しいタブのように、**ヘッダを付けられない**移動のためのURL。
  起動トークンはクエリでも受け付けるので、そこに載せる
  (`app/__init__.py` の `request.args.get("t")`)。

  トークンはもともとこのページの `window.APP.token` に入っている。
  ここでURLに付けても新しく漏れるものは無い ── 送信先は自分自身で、
  `Referrer-Policy: no-referrer` により外へ持ち出されることもない。
*/
export function tokenUrl(path) {
  return `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(TOKEN)}`;
}

export const api = {
  get: (path) => request(path),
  post: (path, data) => request(path, { method: "POST", body: JSON.stringify(data ?? {}) }),
};
