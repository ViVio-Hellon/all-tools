/*
  health.js — バックエンドとの接続を見張る (基盤仕様書 2.9)

  2つある。役目が違うので分けてある:

    見張り (watch)  … 繋がっているか。切れたら**画面に出す**。
                      黙って古い表示を出し続けない
    心拍   (beat)   … こちらが生きていること**をサーバへ伝える**。
                      途切れるとサーバは「誰も見ていない」と判断して終わる
                      (窓の無いアプリなので、タブを閉じたら終わってほしい)

  **裏に回ったタブの心拍は当てにならない。** ブラウザは見えていないタブの
  タイマーを間引き(Chrome は1分に1回まで)、凍らせることもある(Edge の
  スリープ中のタブ・Chrome の省メモリ)。別の道具で、Excel を見ている
  あいだに心拍が途切れてアプリが終わっていた、が実際に起きた。そこで:

    裏に回る瞬間   … 「隠れます」を sendBeacon で送る(凍らされる直前でも届く)。
                     サーバはその画面が戻るか閉じるまで、自動終了も
                     タブの空き判定もしない(idle_exit / screen_lock)
    表に戻ったとき … すぐ心拍を送り、接続を確かめ、同期の状態を取り直す
                     (visibilitychange / resume / pageshow / online)
    スリープ明け   … 時計の飛びで気づく(下の WAKE_GAP_MS)。表のまま蓋を
                     閉じると、どのイベントも来ないため
*/

import { tokenUrl } from "./api.js";
import { reloadSafely } from "./leave.js";
import * as screen from "./screen.js";

const POLL_MS = window.APP.healthPollMs || 15000;
// 心拍の間隔。**出どころはサーバ**(`idle_exit.HEARTBEAT_MS`)
const HEARTBEAT_MS = window.APP.heartbeatMs || 20000;

// スリープ明けの見分け方。WAKE_TICK_MS ごとに時計を見て、前回から
// WAKE_GAP_MS 以上飛んでいたら「止まっていた」とみなす。**表に居るとき
// だけ**見る ── 裏ではタイマーが間引かれるので、飛ぶのがふつう
const WAKE_TICK_MS = 5000;
const WAKE_GAP_MS = 60000;

// 表に戻ったときに呼ぶもの(`onResume`)。同期の取り直しなどを外から足す
const resumeHandlers = new Set();
let checkNow = null;

let offline = false;
let missed = 0;

/** 続けて何回落ちたら「切れた」と見なすか。1回の取りこぼしでは出さない。 */
const TOLERANCE = 2;

export function watch() {
  const banner = document.getElementById("offline");
  const reconnect = document.getElementById("reconnect");
  if (reconnect) reconnect.addEventListener("click", () => check(true));

  async function check(manual = false) {
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      if (!res.ok) throw new Error(String(res.status));
      const health = await res.json();
      missed = 0;
      if (offline) {
        offline = false;
        if (banner) banner.hidden = true;
      }
      // **この画面を出したのとは別のサーバが答えていたら読み込み直す。**
      //
      // 版が上がっていれば JSON の形が変わっていることがある。版が同じでも、
      // 起動し直されていれば**起動トークンが変わっている** ── 開けっぱなしの
      // タブ(夜のあいだに idle_exit で終わり、朝また起動した、など)は
      // 画面もサーバも無事に見えるのに、押すと全部 403 になる。
      // どちらも読み込み直せば直るので、区別せずにやり直す。
      //
      // **打ちかけがあれば先に訊く**(`leave.js`)。以前は黙って読み込み直し、
      // ダイアログに打っていた連絡や設定の欄がそのまま消えていた。
      // 「あとで」なら、片付いたあとの見張りで読み込み直す(版と pid は
      // 覚え直さない ── 覚え直すと、古い画面のまま気づかなくなる)
      const moved = (window.APP.version && health.version !== window.APP.version)
        || (window.APP.pid && health.pid !== window.APP.pid);
      if (moved) {
        reloadSafely("アプリが起動し直されました(版の更新・自動終了のあとの起動など)。");
        return;
      }
      window.APP.version = health.version;
      window.APP.pid = health.pid;
    } catch (_err) {
      missed += 1;
      if (manual || missed >= TOLERANCE) {
        offline = true;
        if (banner) banner.hidden = false;
      }
    }
  }

  checkNow = check;
  check();
  setInterval(check, POLL_MS);
}

/** 表に戻った・スリープから戻ったときに呼ぶ処理を足す。 */
export function onResume(fn) {
  resumeHandlers.add(fn);
  return () => resumeHandlers.delete(fn);
}

/** いま見えているか。**見えていない = 裏**(最小化・別タブ・隠れた窓)。 */
function visibleNow() {
  return document.visibilityState !== "hidden";
}

export function beat() {
  /**
   * @param {"visible"|"hidden"|"frozen"} state いまの見え方
   * @param {{leaving?: boolean, beacon?: boolean}} opts
   *   leaving … 閉じた合図 / beacon … 閉じる・凍る瞬間なので sendBeacon で送る
   */
  const send = (state, { leaving = false, beacon = false } = {}) => {
    if (closing && !leaving) return;
    // **どの画面からの心拍か**も伝える。閉じたなら次の画面が使えるように
    // 空け、取って代わられていたらその旨が返る(`screen.js`)
    const body = JSON.stringify({ leaving, state, screen_id: screen.id() });
    if (beacon && navigator.sendBeacon) {
      // タブを閉じる・凍らされる瞬間は fetch が届かないことがある。
      // `sendBeacon` はそのあとでも送ってくれる
      navigator.sendBeacon(tokenUrl("/api/alive"),
                           new Blob([body], { type: "application/json" }));
      return;
    }
    fetch("/api/alive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    })
      .then((res) => res.json())
      .then((data) => {
        // 別の画面に取って代わられた。**黙って古い表示を続けさせない**
        if (data && data.active === false) screen.lost();
      })
      .catch(() => {});
  };
  const current = () => (visibleNow() ? "visible" : "hidden");
  // 閉じる途中か。**閉じた合図のあとは何も送らない** ── 閉じるときにも
  // 見え方が変わるので「隠れます」が飛び、閉じた扱いを取り消してしまう
  // (サーバ側でも無視するが、送らないのがいちばん確か)
  let closing = false;

  // 表に戻った。**待たずに**心拍を送り、繋がっているかを確かめ直す。
  // 裏にいたあいだに起き直したサーバなら、ここで読み込み直しになる
  let lastResume = 0;
  const resume = (why) => {
    const now = Date.now();
    if (now - lastResume < 1000) return;       // 同じ瞬間に複数のイベントが来る
    lastResume = now;
    send("visible");
    if (checkNow) checkNow();
    resumeHandlers.forEach((fn) => {
      try { fn(why); } catch (_err) { /* 1つ失敗しても他は呼ぶ */ }
    });
  };

  send(current());
  // 裏でも送り続ける(間引かれても届くぶんは届く)。**見え方も毎回添える**
  setInterval(() => send(current()), HEARTBEAT_MS);

  // スリープ明けを見分けるための、前回の時計(下の見張りが使う)
  let lastTick = Date.now();

  // 裏に回る・表に戻る
  document.addEventListener("visibilitychange", () => {
    // 裏ではタイマーが間引かれて時計が飛ぶ。**戻った瞬間を起点に数え直す**
    // (そうしないと、戻った直後にスリープ明けとも見なして二重に動く)
    lastTick = Date.now();
    if (visibleNow()) resume("visible");
    else send("hidden", { beacon: true });
  });
  // 凍らされる直前・解凍(Page Lifecycle API。対応するブラウザだけ来る)
  document.addEventListener("freeze", () => send("frozen", { beacon: true }));
  document.addEventListener("resume", () => { if (visibleNow()) resume("resume"); });
  // 戻る/進むのキャッシュから戻った。閉じた合図を送ったあとなので名乗り直す
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    closing = false;
    screen.claim().then(() => resume("pageshow"));
  });
  // ネットワークが戻った(スリープ明けに来ることが多い)
  window.addEventListener("online", () => { if (visibleNow()) resume("online"); });

  // スリープ明け。表のまま蓋を閉じると、上のどのイベントも来ない
  setInterval(() => {
    const now = Date.now();
    const gap = now - lastTick;
    lastTick = now;
    if (visibleNow() && gap >= WAKE_GAP_MS) resume("wake");
  }, WAKE_TICK_MS);

  // 閉じた合図。**猶予のあとで終わる**ので、再読込なら心拍が戻って取り消される
  window.addEventListener("pagehide", () => {
    send(current(), { leaving: true, beacon: true });
    closing = true;
  });
}
