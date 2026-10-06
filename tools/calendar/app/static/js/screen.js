/*
  screen.js — この端末で使う画面は1つだけ

  **プロセスの二重起動とは別の話です。** 起動しているのが1つでも、
  ブラウザのタブは何枚でも開けます。2枚開くと、どちらでも登録できて、
  どちらも「自分が見ているものが最新」の顔をします ── 片方で登録して
  もう片方が古い月を出したまま、設定を両方で開いて後から保存したほうが
  黙って勝つ、など。**壊れたようには見えず、どちらが本当か分からない**
  という形で出ます。

  そこで、2枚目は開いた時点で断ります。断りの判断はサーバが持っていて
  (`calendar_app/screen_lock.py`)、ここは名乗って結果を写すだけです。

  **取って代わる道は必ず残します。** 前の画面が異常終了して合図を
  送れなかったとき、何十秒も待たせるほうが現場では困ります。
*/

// **`api.js` を使いません。** あちらが要求ごとに画面の名前を載せるため、
// ここから呼ぶと相互参照になります。名乗りは1本の要求なので素で投げます。

// タブごとの名前。**`sessionStorage` に持つ** ── タブごとに別で、
// 画面遷移(カレンダー↔設定)では変わらないので、「同じタブの中で
// 移動しただけ」と「もう1枚開いた」を取り違えません。
const KEY = "screenId";

let cached = "";
// 一度でも「使ってよい」と言われたか。**言われ方を変えるために持つ** ──
// 2枚目として断られたのか、使っていたのに取って代わられたのかで、
// 読む人にとっての意味が違う
let held = false;
// 先に使っている画面がいつからか。**覚えておく** ── 心拍の応答は
// 「外れた」しか教えてくれないので、名乗ったときに聞いた分を使い回す
let holderSince = "";

export function id() {
  if (cached) return cached;
  try {
    cached = sessionStorage.getItem(KEY) || "";
    if (!cached) {
      cached = newId();
      sessionStorage.setItem(KEY, cached);
    }
  } catch (_err) {
    // プライベートウィンドウ等で使えないことがある。
    // **そのときは毎回新しい名前**(遷移のたびに名乗り直すだけで動く)
    cached = cached || newId();
  }
  return cached;
}

function newId() {
  if (window.crypto && window.crypto.randomUUID) return crypto.randomUUID();
  return `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/**
 * この画面で使わせてほしいと名乗る。
 * @returns {Promise<boolean>} 使ってよければ `true`
 */
export async function claim({ force = false } = {}) {
  try {
    const res = await fetch("/api/screen/claim", {
      method: "POST",
      cache: "no-store",
      headers: {
        "X-Tool-Token": window.APP.token,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ screen_id: id(), force }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok) {
      held = true;
      hide();
      return true;
    }
    if (res.status === 409) {
      if (body.holder_since) holderSince = body.holder_since;
      show(body);
      return false;
    }
    // 名乗れないことを理由に画面を止めない。**守りはサーバ側にもある**
    return true;
  } catch (_err) {
    return true;
  }
}

/** 心拍の応答で「外れた」と分かったときに呼ぶ。 */
export function lost() {
  // **まだ一度も使っていないなら「2枚目だった」。** 心拍は名乗りと
  // 同時に飛ぶので、順番しだいでこちらが先に返ることがある
  show({ taken: held });
}

/* ------------------------------------------------------------------
   断りの面

   **操作できないことが見て分かるようにする。** 薄く隠すだけだと、
   押せないボタンを押し続けることになります。
   ------------------------------------------------------------------ */
function show(info) {
  const box = document.getElementById("screen-block");
  if (!box) return;
  const title = document.getElementById("screen-block-title");
  const text = document.getElementById("screen-block-text");
  const take = document.getElementById("screen-block-take");

  if (info.taken) {
    if (title) title.textContent = "別の画面で開かれました";
    if (text) {
      text.textContent = "この画面では操作できません。"
        + "こちらで続ける場合は下を押してください。";
    }
    if (take) take.textContent = "この画面に戻す";
  } else {
    if (title) title.textContent = "すでに別の画面で開いています";
    if (text) {
      const from = info.holder_since || holderSince;
      const since = from ? `(${from} から)` : "";
      text.textContent = `この端末では、別のタブでこのツールを開いています${since}。`
        + "2つ開くと、どちらが最新か分からなくなるため、こちらは操作できません。"
        + "先に開いている画面を使うか、下を押してこちらへ切り替えてください。";
    }
    if (take) take.textContent = "この画面で使う";
  }
  box.hidden = false;
}

function hide() {
  const box = document.getElementById("screen-block");
  if (box) box.hidden = true;
}

/** 「この画面で使う」を張る。押されたら取って代わって読み込み直す。 */
export function wireTakeOver() {
  const take = document.getElementById("screen-block-take");
  if (!take) return;
  take.addEventListener("click", async () => {
    if (await claim({ force: true })) location.reload();
  });
}
