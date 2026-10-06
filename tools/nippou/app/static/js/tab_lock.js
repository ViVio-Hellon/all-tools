/*
  tab_lock.js — 打てるタブは1枚だけ

  【何が起きていたか】
  このアプリはブラウザで動くので、**タブは指1本で増えます。** 同じ日報を
  2枚開くとどちらにも打ててしまい、保存すると後から押したほうが勝ちます。

      タブA: 1〜6行目を打って保存   → DBには6行
      タブB: (Aを開く前の画面のまま)
             7行目だけ打って保存    → Bが持っていた形で上書き
                                     → **Aの6行が消える**

  消えたことは誰にも見えません。どちらのタブも正常に見えています ──
  アプリを2つ起動したときとまったく同じ壊れ方で、こちらのほうがずっと
  起きやすい。

  【ここがやること】
  開いたらサーバに名乗り、5秒ごとに心拍を送ります。打てる側でないと
  言われたら、**日報入力の画面をまるごと1枚に差し替えます。** 打ちたければ
  「このタブで入力する」を押す ── 勝手には移しません(打っている最中に
  後ろのタブへ権利が移ると、足元が崩れます)。

  【なぜ帯ではなく、1枚に差し替えるのか】
  はじめは帯を出して入力欄を触れなくしていました。**それでも紛らわしい**
  と言われたところです ── 12行の表も、etc のボタンも、保存の並びも、
  薄くなっただけで**そこにあります。** 画面としては日報入力のままなので、
  どちらのタブで打っているのかは、結局その帯を読まないと分かりません。

      2枚目を開いた時点で「別のタブで開いています」の1枚に差し替える

  打つものが1つも無い画面にすれば、読まなくても分かります。中身は
  消していません ── 隠しているだけなので、権利が戻れば元に戻ります。

  **他の画面までは差し替えません。** 取り合うのは「打つ画面」だけなので、
  記録・グラフ・集計は2枚目でもふつうに見られます(`onLeave` で戻します)。

  **画面を差し替えるのは半分です。** 差し替えたのは見た目で、要求そのものは
  止まっていません(戻る/進む、開きっぱなしの古いタブ、二重送信)。
  もう半分はサーバが持っています(`app/__init__.py` の `_register_tab_guard`)。

  【裏に回ったら、そう伝える】(v3.79.0)
  ブラウザは見えていないタブのタイマーを間引きます(Chrome は隠れて5分で
  1分に1回、Edge のスリープ中のタブは止める)。5秒ごとの心拍が途切れると、
  サーバは15秒で「閉じられた」とみなし、**押してもいないのに**権利が
  ほかのタブへ移っていました。隠れる瞬間に `/api/tab/hide` を送れば、
  サーバは心拍が来なくても権利を持たせたままにします。心拍にも
  `hidden` を添えます(間引かれて届いたぶんで「表に戻った」と
  誤解させない)。

  表に戻った・スリープから起きたときは `health.js` が `app:resume` を
  出すので、**待たずに**心拍を送り直します(次の5秒を待つと、そのあいだ
  古い役のまま打ててしまう)。

  【引き継いだら、表を読み直す】(v3.91.0)
  打てる側のタブを閉じると、見るだけだったタブが次の心拍で打てる側に
  なります。そのタブの表は**開いた時点の中身**で、閉じたタブがあとから
  保存した行を知りません ── そのまま打つと自動保存が古い表で上書きし、
  **閉じたタブの行が消えていました。** サーバは「このタブの表を描いた
  あとに、ほかのタブが書いている」を心拍の答え(`stale`)で返すので、
  打てる側になってそれが立っていたら読み直します。書き込みの口も同じ
  判断で断ります(`stale_tab`。`api.js` が読み直す)。

  【差し替えるのは日報入力の上にいるときだけ】(v3.91.0)
  心拍はほかの画面へ移っても送り続けます(権利を落とさないため)。その
  答えで役が変わったとき、**グラフや記録の画面まで**「別のタブで開いて
  います」の1枚に差し替えていました。取り合うのは打つ画面だけです。
*/
import { api, background, tabId, tabReady } from "./api.js";
import { onLeave } from "./nav.js";
import { toast } from "./toast.js";

/** 差し替える1枚の置き場所。無ければ作る。 */
const BANNER_ID = "tab-lock";

let timer = null;
let role = "editor";
let onChange = null;
/** いま日報入力の画面の上にいるか。**差し替え・読み直しはここだけ** */
let onEntry = false;
/** 読み直したあとに出す一言の控え(`api.js` と同じ鍵) */
const RELOADED_KEY = "nippou.tab.reloaded";

/** 表が古いので読み直す。**読み直した画面で理由を出す。** */
function reloadStale(message) {
  try { sessionStorage.setItem(RELOADED_KEY, message); } catch (err) { /* 出せないだけ */ }
  location.reload();
}

function banner() {
  let box = document.getElementById(BANNER_ID);
  if (box) return box;
  box = document.createElement("div");
  box.id = BANNER_ID;
  box.className = "card tab-lock";
  box.setAttribute("role", "alert");
  box.hidden = true;
  box.innerHTML =
    '<div class="tab-lock__text">'
    + '<h2>この日報は、別のタブで開いています</h2>'
    + '<p class="lead">打てるのは<b>1つのタブだけ</b>です ── 2枚に打つと、'
    + '<b>後から保存したほうで上書きされ、先に打ったぶんが消えます。</b>'
    + '</p>'
    + '<p class="lead">打っているタブへ戻ってください。'
    + 'そちらを閉じてしまった・どれか分からないときは、'
    + '下のボタンでこのタブへ移せます'
    + '(<b>移した先の中身はDBから読み直します</b>)。</p>'
    + '<p class="lead tab-lock__note" hidden></p>'
    + '</div>'
    + '<div class="btn-row">'
    + '<button class="btn btn--primary btn--lg" type="button" id="tab-lock-take">'
    + 'このタブで入力する</button>'
    + '<span class="lead">記録・グラフ・集計は、このタブでも見られます</span>'
    + '</div>';
  // 画面のいちばん上へ。**見落とされる場所に置かない**
  const main = document.querySelector("main") || document.body;
  main.insertBefore(box, main.firstChild);
  box.querySelector("#tab-lock-take")?.addEventListener("click", take);
  return box;
}

/** 打てない側の見た目。**画面ごと差し替える。** */
function paint() {
  // 日報入力を出ている(グラフ・記録など)。**そちらは差し替えない** ──
  // 役はここで覚えておき、日報入力へ戻ったときに `start()` が名乗り直す
  if (!onEntry) return;
  const viewer = role !== "editor";
  const box = banner();
  box.hidden = !viewer;
  paintNote();
  // 本体を隠すのは CSS(`body.is-viewer #main > *:not(#tab-lock)`)。
  // JS で1つずつ `display` を触ると、戻すときに元の値が分からない
  document.body.classList.toggle("is-viewer", viewer);
  // 隠れていても**押せないようにしておく。** 隠すのは見た目の話で、
  // 焦点はキーボードで入り込めますし、古い要求も飛べます。
  //
  // **触るのは `#main` の中だけ。** 帯の「終了」や左の並びまで灰色に
  // すると、このタブが壊れたように見えます ── そちらは打つものでは
  // ないので、2枚目でもふつうに使えるのが正しい。
  const scope = document.getElementById("main") || document.body;
  for (const el of scope.querySelectorAll(
      "input, select, textarea, button")) {
    if (el.closest(`#${BANNER_ID}`)) continue;
    if (viewer) {
      if (!el.disabled) el.dataset.tabLocked = "1";
      el.disabled = true;
    } else if (el.dataset.tabLocked) {
      delete el.dataset.tabLocked;
      el.disabled = false;
    }
  }
  if (onChange) onChange(role);
}

function apply(body) {
  note = (body && body.note) || "";
  paintNote();
  const next = (body && body.role) || "editor";
  // **打てる側なのに表が古い**(引き継いだ・戻る/進むで出てきた)。
  // 打ち始める前に読み直す ── 打ってからでは、打ったぶんが入らない
  if (next === "editor" && body && body.stale && onEntry) {
    reloadStale("前のタブで保存された内容を読み直しました。このタブで続けて打てます");
    return;
  }
  if (next === role) return;
  role = next;
  paint();
}

/** 打てるタブが裏に回っているときの一言。**文言はサーバが持つ。** */
let note = "";

function paintNote() {
  const el = document.getElementById(BANNER_ID)?.querySelector(".tab-lock__note");
  if (!el) return;
  el.textContent = note;
  el.hidden = !note;
}

async function take() {
  try {
    apply(await api.post("/api/tab/take", {}));
    // 奪ったら、いまの中身をサーバの見ているものに合わせ直す ──
    // 見ていたあいだに、打てる側が保存しているかもしれない
    location.reload();
  } catch (err) {
    console.warn("[tab] 入力を引き受けられません:", err);
  }
}

function hiddenNow() {
  return document.visibilityState === "hidden";
}

/** この表を描いた時刻(`entry.html` の `data-tab-loaded`)。 */
function loadedAt() {
  const raw = document.getElementById("sheet-key")?.dataset.tabLoaded;
  const value = Number(raw);
  return raw && Number.isFinite(value) ? value : null;
}

async function beat(path) {
  try {
    // **名札が決まってから名乗る。** 複製したタブは名札を作り直すので
    // (`api.js` の `tabReady`)、待たずに名乗ると元のタブの名札で名乗る
    await tabReady;
    const body = { hidden: hiddenNow() };
    if (path === "/api/tab/claim") body.loaded = loadedAt();
    apply(await background.post(path, body));
  } catch (err) {
    // 心拍が届かないのは、アプリが落ちた・入れ替え中など。**打てなく
    // しない** ── 打てなくすると、原因の分からないまま入力が止まる
    console.warn("[tab] 心拍を送れません:", err);
  }
}

/**
 * この画面で見張りを始める。**日報入力だけが呼びます。**
 *
 * グラフや集計を2枚目で開くのはふつうの使い方なので、そこまで止めると
 * 邪魔になるだけです。取り合うのは「打つ画面」だけ。
 */
let every = 5000;
let started = false;

export function start(options = {}) {
  onChange = options.onChange || null;
  stop();
  role = "editor";
  started = true;
  onEntry = true;
  // 読み直した理由(表が古かった)。**読み直した先で言う**
  try {
    const why = sessionStorage.getItem(RELOADED_KEY);
    if (why) {
      sessionStorage.removeItem(RELOADED_KEY);
      toast(why, "warn");
    }
  } catch (err) { /* 出せないだけ */ }
  beat("/api/tab/claim");
  every = (options.heartbeatSec || 5) * 1000;
  timer = setInterval(() => beat("/api/tab/ping"), every);
  listen();

  // **この画面を出たら、差し替えを戻す。** 記録やグラフまで1枚に
  // 差し替えたままにすると、2枚目のタブでは何も見られなくなります
  // ── 取り合っているのは「打つ画面」だけです。
  //
  // 心拍は止めません(`unpaint` であって `stop` ではない)。止めると、
  // グラフを見に行って戻ってきただけで打つ権利を落とします。
  onLeave(() => {
    onEntry = false;
    unpaint();
  });
}

let listening = false;

/** ページの出入りの合図を拾う。**1度だけ掛ける**(画面を出入りしても増やさない)。 */
function listen() {
  if (listening) return;
  listening = true;
  // 閉じたら、待たずに次のタブへ渡す。`sendBeacon` はヘッダを付けられ
  // ないので、合言葉はクエリ、タブの名札は本文に載せる(相手は自分自身)
  window.addEventListener("pagehide", release);
  // 戻る/進むの控え(bfcache)から戻った。閉じた合図で明け渡しているので
  // **名乗り直す**(止めたタイマーも掛け直す)
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted || !started) return;
    beat("/api/tab/claim");
    if (!timer) timer = setInterval(() => beat("/api/tab/ping"), every);
  });
  // 裏に回った。**権利は持ったまま**、心拍が止まっても外さないでもらう
  document.addEventListener("visibilitychange", () => {
    if (hiddenNow() && timer) signal("/api/tab/hide");
  });
  // 表に戻った・凍結が解けた・スリープから起きた(`health.js` が出す)。
  // 次の5秒を待たずに役を確かめ直す
  window.addEventListener("app:resume", () => {
    if (timer) beat("/api/tab/ping");
  });
}

/** 閉じ際・隠れ際の合図。`sendBeacon` はヘッダを付けられない。 */
function signal(path) {
  try {
    const url = `${path}?t=${encodeURIComponent(window.APP.token)}`;
    const body = JSON.stringify({ tab: tabId });
    const queued = navigator.sendBeacon?.(url, new Blob([body], { type: "application/json" }));
    // デスクトップ版(統合ツールの窓)の中では、離れるページの `sendBeacon` が
    // 届かないことがある(看板のデスクトップ版で実際に起きた)。送れなかったとき・
    // 窓の中のときは、`keepalive` 付きの送り直しも出す(受ける側は2回来ても同じ)
    if (!queued || window.__TAURI__) {
      fetch(url, { method: "POST", body, keepalive: true,
                   headers: { "Content-Type": "application/json" } }).catch(() => {});
    }
  } catch (err) {
    /* 失敗しても何もできない(閉じたなら15秒で自然に外れる) */
  }
}

/** 差し替えだけ戻す。**心拍も権利もそのまま。** */
function unpaint() {
  document.body.classList.remove("is-viewer");
  document.getElementById(BANNER_ID)?.remove();
}

function release() {
  if (!timer) return;                   // 見張っていない(日報入力を開いていない)
  clearInterval(timer);
  timer = null;
  signal("/api/tab/release");
}

export function stop() {
  if (timer) { clearInterval(timer); timer = null; }
  unpaint();
}

/** いまこのタブは打てるか。保存の前に見る。 */
export function mayEdit() {
  return role === "editor";
}
