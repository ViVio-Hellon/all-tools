/*
  api.js — サーバとのやりとり

  JSが持つのは「呼ぶ」「受け取ったものを描く」だけ。
  **業務の判断はサーバが行う**(押せるか・隠すか・何と出すか)。
*/

import * as busy from "./busy.js";

const TOKEN = window.APP.token;

/*
  このタブの名札。**タブごとに別**で、読み直しても変わらない。

  同じ日報を2枚のタブで開くと、どちらにも打ててしまい、後から保存した
  ほうが勝ちます ── 先に打った行は黙って消えます。サーバは「いま打てる
  のはどのタブか」を1つだけ覚えていて、書き込みの口はこの名札で見ます。

  `sessionStorage` はタブごとに分かれていて、読み直しでは消えません ──
  ちょうど「タブ1枚」の寿命です。`localStorage` だと全タブで同じものに
  なり、`window` 変数だとF5で別のタブ扱いになります。

  【複製したタブは、名札まで写ってくる】(v3.91.0)
  ブラウザは「タブを複製」や `window.open` で開いたタブに、元のタブの
  `sessionStorage` を**写して**渡します。名札も同じものになるので、
  サーバからは1枚に見え、**2枚とも打てる側**になっていました。

  そこで開いたときに、同じブラウザの他のタブへ「この名札を使っている
  タブはいますか」と訊きます(`BroadcastChannel`)。返事があれば、
  自分が写してもらった側なので名札を作り直します。F5 で読み直した
  タブは、前の自分がもう居ないので返事が無く、名札はそのままです。
*/
const TAB_KEY = "nippou.tab";

function newTabId() {
  return (crypto.randomUUID && crypto.randomUUID())
         || String(Date.now()) + Math.random().toString(16).slice(2);
}

let TAB = (() => {
  try {
    let id = sessionStorage.getItem(TAB_KEY);
    if (!id) {
      id = newTabId();
      sessionStorage.setItem(TAB_KEY, id);
    }
    return id;
  } catch (err) {
    // 保存を断られる場面(プライベート閲覧など)。**名札は必ず返す** ──
    // 無いと全部の書き込みが「どのタブか分からない」になる
    return newTabId();
  }
})();

/** このタブの名札。**作り直すことがある**ので、使うたびに読む。 */
export let tabId = TAB;

/** 返事を待つ長さ(ms)。同じブラウザの中なので、届くならすぐ届く */
const ASK_MS = 150;

/**
 * 名札が決まったら解ける約束。**名乗る(`/api/tab/claim`)前に待つ。**
 * 待たずに名乗ると、複製したタブが元のタブの名札で打てる側になります。
 */
export const tabReady = (() => {
  if (typeof BroadcastChannel !== "function") return Promise.resolve(TAB);
  const me = newTabId();                  // この読み込み1回ぶんの呼び名
  const channel = new BroadcastChannel("nippou-tab");
  return new Promise((resolve) => {
    let settled = false;
    const settle = () => { if (!settled) { settled = true; resolve(TAB); } };
    channel.addEventListener("message", (event) => {
      const msg = event.data || {};
      if (msg.ask === TAB && msg.from !== me) {
        // 同じ名札で訊かれた。**こちらは前から居る側**として答える
        channel.postMessage({ taken: TAB, to: msg.from });
      } else if (msg.taken === TAB && msg.to === me && !settled) {
        // 同じ名札のタブが居た。**写してもらった側なので作り直す**
        TAB = newTabId();
        tabId = TAB;
        try { sessionStorage.setItem(TAB_KEY, TAB); } catch (err) { /* 名札は持っている */ }
        settle();
      }
    });
    channel.postMessage({ ask: TAB, from: me });
    setTimeout(settle, ASK_MS);
  });
})();

/**
 * サーバが返したエラーを、そのまま画面に出せる形で持つ。
 *
 * 断りの返し方は2通りある。**どちらでも文言はサーバが持っている**ので、
 * 両方から拾う:
 *
 *   入力の形の誤り   … `{"error": {"code", "message"}}`   状態は動いていない
 *   業務としての断り … 画面ぜんぶ + 最上位の `message`     状態は動いていることがある
 *
 * 後者を拾い忘れると、理由(「先に報告日を選んでください。」)の代わりに
 * 「通信に失敗しました (HTTP 422)」と出る ── **通信は成功しているのに
 * 通信の失敗として案内される**ので、現場は直しようがない。
 */
export class ApiError extends Error {
  constructor(status, body) {
    const info = (body && body.error) || {};
    super(info.message || (body && body.message)
          || `通信に失敗しました (HTTP ${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = info.code || "";
    this.field = info.field || "";
    // 予期しないエラーの番号(サーバが付けたもの)。文言にも入っている
    this.eventId = info.event_id || "";
    // 断られたときも本文に画面ぜんぶが入っていることがある(422)。
    // 「断られた」と「画面が古いまま」を同時に起こさないために渡す
    this.body = body;
  }
}

async function request(path, options = {}, { quiet = false, mute = false } = {}) {
  /*
    **押した本人に、いま待っていることを伝える。**
    伝える相手は `busy.watchClicks()` が覚えている「いま押されたボタン」。

    `quiet` は**誰も押していない通信**(1分ごとの見張りなど)。あれは
    覚えている「直前に押されたボタン」が誰であれ無関係なので、待機の姿を
    出してはいけません ── 出すと、とっくに終わった操作のボタンが
    背後の通信のたびに勝手に無効化されます。
  */
  const done = quiet ? () => {} : busy.mark(busy.pressed());
  try {
    const res = await fetch(path, {
      ...options,
      cache: "no-store",
      headers: {
        "X-Tool-Token": TOKEN,
        // どのタブからの要求か。書き込みの口がこれを見る
        "X-Tab": TAB,
        // **どの画面から**押したか。記録(なぜなぜの「どの画面で」)に残る
        "X-Screen": location.pathname,
        // 誰も押していない通信(見張り)。記録に操作として残さない
        ...(quiet ? { "X-Quiet": "1" } : {}),
        // `FormData` のときは**付けない**。境界文字列つきのヘッダを
        // ブラウザが組み立てるので、こちらで上書きすると中身が読めない
        ...(options.body && !(options.body instanceof FormData)
            ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });

    let body = null;
    const type = res.headers.get("Content-Type") || "";
    if (type.includes("application/json")) {
      body = await res.json();
    }
    if (!res.ok) {
      const error = new ApiError(res.status, body);
      if (error.code === "stale_tab") reloadStale(error.message);
      // 押した操作が断られた/エラーになった ── 音の出来事(`sound.js`)。
      // 見張りの通信(quiet)と、押さずに走るもの(mute: 自動保存)では鳴らさない
      else if (!quiet && !mute) {
        announce("api:failed", { status: res.status, body, eventId: error.eventId });
      }
      throw error;
    }
    // サーバが「この出来事が起きた」と添えてきた(保存した・共有へ保存した)
    if (body && body.sound_cue) announce("sound:cue", body.sound_cue);
    return body;
  } finally {
    done();
  }
}

/*
  ほかの部品へ知らせる(音など)。**ここから音の部品を直接呼ばない** ──
  `sound.js` はこのファイルを使うので、呼び合うと読み込みの順が絡みます。
*/
function announce(type, detail) {
  try {
    window.dispatchEvent(new CustomEvent(type, { detail }));
  } catch (err) { /* 知らせられなくても通信の結果は返す */ }
}

/**
 * 表が古いと断られた(`stale_tab`)。**読み直す。**
 *
 * この画面を描いたあとに別のタブが保存しています。このまま打ち続けると、
 * 次の保存でも断られるだけで、打ったものは入りません。読み直した画面で
 * 断りの文言を出せるよう、控えに置いてから読み直します(`tab_lock.js`)。
 */
function reloadStale(message) {
  try { sessionStorage.setItem("nippou.tab.reloaded", message); } catch (err) { /* 出せないだけ */ }
  location.reload();
}

/*
  `<img src>` や `<iframe src>` のように、**ヘッダを付けられない**読み込みの
  ためのURL。起動トークンはクエリでも受け付けるので、そこに載せる。

  トークンはもともとこのページの `window.APP.token` に入っている。
  ここでURLに付けても新しく漏れるものは無い ── 送信先は自分自身で、
  `Referrer-Policy: no-referrer` により外へ持ち出されることもない。
*/
export function tokenUrl(path) {
  return `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(TOKEN)}`;
}

export const api = {
  get: (path) => request(path),
  /**
   * `mute` … 断られても音の出来事(「断られた」)にしない。**押さずに走るもの**
   * (自動保存)のため。待機の姿や記録は押したときと同じ
   *
   * `signal` … 画面を出たら取り消す合図(`nav.pageSignal()`、v4.24.0)。
   * **出たあとに返ってきた答えを、次の画面へ塗らせない**ため ── 前のページの
   * 12行が、引き直したばかりの新しいページへ塗られ、自動保存で書かれていました。
   * 取り消されると `AbortError` で落ちます(`isAbort` で見分ける)
   */
  post: (path, data, { mute = false, signal } = {}) =>
    request(path, { method: "POST", body: JSON.stringify(data ?? {}),
                    ...(signal ? { signal } : {}) }, { mute }),
  /**
   * ファイルそのものを送る(取り込み)。
   *
   * **`Content-Type` は付けません。** `FormData` を渡すと境界文字列つきの
   * ヘッダをブラウザが組み立てます ── こちらで `application/json` を
   * 付けてしまうと、サーバは中身を読めません。
   */
  send: (path, form) =>
    request(path, { method: "POST", body: form, headers: { "X-Form": "1" } }),
  /**
   * 閉じる・読み直す間際に送る(`keepalive`)。返事は待たない ── 画面はもう無い
   */
  beacon: (path, data) => {
    try {
      fetch(path, {
        method: "POST", keepalive: true, cache: "no-store",
        headers: { "X-Tool-Token": TOKEN, "X-Tab": TAB, "X-Screen": location.pathname,
                   "Content-Type": "application/json" },
        body: JSON.stringify(data ?? {}),
      }).catch(() => {});
    } catch (err) { /* 送れなくても閉じるのは止めない */ }
  },
};

/** 画面を出たので取り消した通信か(`signal`)。**失敗として出さない。** */
export function isAbort(err) {
  return Boolean(err) && (err.name === "AbortError" || err.code === 20);
}

/**
 * 誰も押していない通信(1分ごとの見張りなど)。
 *
 * 中身は `api` と同じで、**待機の姿を出さない**ところだけが違います。
 */
export const background = {
  get: (path) => request(path, {}, { quiet: true }),
  post: (path, data) =>
    request(path, { method: "POST", body: JSON.stringify(data ?? {}) },
            { quiet: true }),
};
