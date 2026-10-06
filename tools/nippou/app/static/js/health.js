/*
  health.js — バックエンドが生きているかの見張り (基盤仕様書 2.9)

  ブラウザ画面が開いていることと、Pythonバックエンドが正常に動いている
  ことは別。**黙って古い表示を出し続けない**ため、繋がらなくなったら
  画面上で明示し、再接続の操作を出す。

  この見張りは画面を移っても止めない(`nav.js` は外枠を残す)。
*/
import { tabId } from "./api.js";

const POLL_MS = window.APP.healthPollMs || 15000;

let offlineSince = 0;

function banner() {
  return document.getElementById("offline");
}

function show(isOffline) {
  const node = banner();
  if (!node) return;
  if (isOffline === !node.hidden) return;      // 変化なし
  node.hidden = !isOffline;
}

async function ping() {
  try {
    // **トークンを付けない。** `/api/health` は素通しにしてあるので、
    // トークンが切れていても生死だけは分かる
    const res = await fetch("/api/health", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();

    // 同じポートで**別のアプリ**が動いている場合。繋がってはいるが、
    // 自分のバックエンドではない
    if (body.app_id && window.APP.appId && body.app_id !== window.APP.appId) {
      show(true);
      return;
    }
    offlineSince = 0;
    show(false);
  } catch (err) {
    if (!offlineSince) offlineSince = Date.now();
    show(true);
  }
}

/* ================================================================
   こちらが生きていることを伝える (基盤仕様書 2.8「自動終了」)

   **窓が無いアプリなので、タブを閉じたら終わったつもりになる。**
   ところが Python は動いたままで、次の起動が「すでに起動しています」
   と判定し、入れ替えた新しい版がいつまでも動かない。

   そこで、開いているあいだは心拍を送る。閉じたことは `sendBeacon` で
   即座に伝える ── 閉じる瞬間の `fetch` は破棄されることがあるが、
   `sendBeacon` はブラウザが送りきってくれる。

   **落とす判断はサーバがする。** ここは「居る/裏に回った/出ていく」を
   伝えるだけで、猶予も、処理中かどうかも、1度も繋がっていないかも見ない
   (`nippou/idle_exit.py`)。

   【裏に回ったら、そう伝える】(v3.79.0)
   ブラウザは**見えていないタブのタイマーを間引く**(Chrome は隠れて
   5分たつと1分に1回。Edge のスリープ中のタブやメモリセーバーは止める)。
   Excel を全画面にした・窓を最小化した、だけで見えていない扱いになり、
   止められたタブからは心拍が来ない ── サーバは90秒で「誰も見ていない」
   と判断して終わっていた。

   だから**隠れる瞬間に `hidden` を送る**(`sendBeacon`。この先ここの
   タイマーが動く保証は無い)。サーバは、裏に回ったタブは心拍が止まっても
   居るものとみなす。心拍そのものも止めず、「いま裏です」と言い添えて送る。

   【戻ってきたことに、自分で気づく】
   表に戻った・凍結が解けた・戻る/進むで戻った・スリープから起きた、の
   どれでも同じ `wake()` を走らせる: すぐ心拍を送り、生きているかを確かめ、
   ほかの見張り(打てるタブ・直の変わり目)へ `app:resume` で知らせる。
   次の周期(最大20秒・直は1分)まで待つと、そのあいだ古い状態のまま
   操作できてしまう。

   スリープは**タイマーの空きで気づく**。5秒ごとの見張りが30秒以上
   空いて起きたら、そのあいだ止まっていた。見えているタブのときだけ
   見る ── 裏のタブは間引かれて空くのが当たり前なので。
   ================================================================ */
const ALIVE_MS = window.APP.alivePollMs || 20000;

// スリープに気づくための見張りの間隔と、「止まっていた」とみなす空き(ms)
const WAKE_TICK_MS = 5000;
const WAKE_GAP_MS = 30000;

// 閉じた合図を送ったあと。**凍結(`freeze`)の合図で取り消さない** ──
// 戻る/進むの控え(bfcache)に入るときは pagehide → freeze の順で来る
let left = false;

// 読み込みごとのページ番号。**閉じたページから遅れて着いた合図**で
// サーバがタブを生き返らせないための印。閉じるときブラウザは
// 「裏に回った」と「閉じた」を続けて出し、着く順は決まっていない
// (`nippou/idle_exit.py`)。名札(tabId)は再読込でも同じなので別に持つ
function newPage() {
  return (crypto.randomUUID && crypto.randomUUID())
    || String(Date.now()) + Math.random().toString(16).slice(2);
}
let page = newPage();

function visibility() {
  return document.visibilityState === "hidden" ? "hidden" : "visible";
}

function beacon(body) {
  const payload = JSON.stringify({ tab: tabId, page, ...body });
  if (navigator.sendBeacon
      && navigator.sendBeacon("/api/alive",
        new Blob([payload], { type: "application/json" }))) {
    return;
  }
  send(payload);
}

function send(payload) {
  // **返ってきた中身は読み捨てる。** 読まないと本体の流れが開いたままで、
  // ブラウザは「まだ終わっていない要求」として接続を抱え続ける
  fetch("/api/alive", {
    method: "POST", cache: "no-store", keepalive: true,
    headers: { "Content-Type": "application/json" }, body: payload,
  }).then((res) => res.text())
    .catch(() => { /* 届かなくても画面は続く。次の心拍で取り戻す */ });
}

function alive() {
  if (left) return;
  // 裏に回っていても送る(間引かれても届くぶんは届く)。**いまどちらか
  // を毎回言い添える** ── 隠れた合図が届かなかったときの取り戻し
  send(JSON.stringify({ tab: tabId, page, state: visibility() }));
}

let lastWake = 0;

/** 戻ってきた。すぐ心拍を送り、確かめ、ほかの見張りへ知らせる。 */
function wake(why, gapMs = 0) {
  left = false;
  const now = Date.now();
  // 表に戻る・凍結が解ける・フォーカスが重なって来ることがある。1回に束ねる
  if (now - lastWake < 1000) return;
  lastWake = now;
  alive();
  ping();
  window.dispatchEvent(new CustomEvent("app:resume",
    { detail: { why, gapMs } }));
}

function hidden(why) {
  if (left) return;
  beacon({ state: "hidden", why });
}

function startAlive() {
  alive();
  setInterval(alive, ALIVE_MS);

  // 裏に回った/表に戻った
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") hidden("hidden");
    else wake("visible");
  });
  // ブラウザがタブを凍結する/解く(Chrome の Page Lifecycle)。
  // 凍結の前にはたいてい隠れた合図が出ているが、念のためもう1度
  document.addEventListener("freeze", () => hidden("freeze"));
  document.addEventListener("resume", () => wake("resume"));

  // 閉じた。`pagehide` は再読込でも飛ぶが、**戻ってくれば次の心拍で
  // 取り消される**(サーバ側が猶予を持っている)。戻る/進むの控えに
  // 入るとき(persisted)も、このタブはアプリを離れたので同じ扱い
  window.addEventListener("pagehide", () => {
    left = true;
    beacon({ leaving: true });
  });
  // 戻る/進むで控えから戻った。止まっていたタイマーごと戻ってくる。
  // 閉じた合図を送ったページ番号は使えないので、**番号を取り直す**
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    page = newPage();
    wake("pageshow");
  });
  // 回線が戻った(アダプタの切り替え・スリープ明け)
  window.addEventListener("online", () => wake("online"));

  // スリープに気づく。**見えているあいだだけ**タイマーの空きで見る
  let last = Date.now();
  setInterval(() => {
    const now = Date.now();
    const gap = now - last;
    last = now;
    if (gap > WAKE_GAP_MS && document.visibilityState === "visible") {
      wake("sleep", gap);
    }
  }, WAKE_TICK_MS);
}

export function startHeartbeat() {
  const node = banner();
  if (node) {
    const btn = document.getElementById("reconnect");
    if (btn) btn.addEventListener("click", () => ping());
  }
  ping();
  setInterval(ping, POLL_MS);

  // タブに戻ってきた瞬間に確かめるのは `wake()`。放置していたあいだに
  // 落ちていることがあり、次の周期まで気づけないと古い画面を操作してしまう
  startAlive();
}
