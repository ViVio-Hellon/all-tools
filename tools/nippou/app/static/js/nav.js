/*
  nav.js — 画面を移っても外枠を作り直さない

  【SPA にはしない。それでも再読込はしない】
  ルータも、画面側の状態管理も、ビルド工程も入れない。入れるのは1つだけ:

      行き先のHTMLをサーバから貰って、**中身だけ差し替える**。

  ・画面の中身を決めるのは今まで通りサーバ(判断はPython)
  ・画面が新しく持つ状態は無い。URL とサーバの応答が唯一の出どころ
  ・npm も webpack も要らない。`pip install -r requirements.txt` のまま

  【なぜ差し替えるのか】
  日報入力 → 全停入力 → 集計 …と行き来する。**そのたびに白い瞬間が
  挟まる**と、出したばかりのトーストが消え、接続断の帯が一瞬消えてまた
  出て、押した場所が作り直されて指の位置がずれる。速さの話ではなく、
  **同じ画面の中に居るという感覚**が切れることが問題になる。

  【差し替えるもの / 残すもの】
      差し替える … <title> / 画面ごとの <style> / リボン / レール /
                    <main> / 画面ごとの <script>
      残す       … 接続断の帯・トースト・心拍(health.js)・app.js 本体

  【失敗したら普通に移る】
  取りに行けない・形が違う・古いブラウザ ── どの場合も
  `location.href` へ落とす。**移れなくなることは無い。**
*/

/** 差し替えたい要素。この順に上から入れ替える。 */
const SWAP = ["header.ribbon", "nav.rail", "main#main"];

/** 画面ごとの `<script>` を入れておく箱(base.html)。 */
const SCRIPT_BOX = "#pagescripts";

let seq = 0;                       // 何度目の移動か。古い応答は捨てる
let page = new AbortController();  // この画面のあいだだけ有効
let afterSwap = () => {};          // 外枠を繋ぎ直す係(app.js が渡す)

/**
 * その画面のあいだだけ有効な合図。
 *
 * `<main>` の中に付けた listener は差し替えと一緒に消えるが、
 * **`document` / `window` に付けたものとタイマーは消えない。**
 * 消し忘れると、画面を出たあとも動き続けて二重に効く。
 *
 *     document.addEventListener("keydown", fn, { signal: pageSignal() });
 */
export function pageSignal() {
  return page.signal;
}

/** 画面を出るときに1度だけ呼ばれる後始末。タイマーはここで止める。 */
export function onLeave(fn) {
  page.signal.addEventListener("abort", fn, { once: true });
}

/** この場所を差し替えで開けるか。開けないものは普通の遷移に任せる。 */
function internal(url) {
  return url.origin === location.origin
      && !url.pathname.startsWith("/api/")
      && !url.pathname.startsWith("/report/")
      && !url.pathname.startsWith("/static/")
      && !url.pathname.startsWith("/sv/");
}

/**
 * `<a>` を押したときに差し替えで移る。
 *
 * 修飾キー付き(新しいタブで開く)・右クリック・`target` 付き・
 * ダウンロードは**触らない**。利用者がブラウザの機能として期待して
 * いるものを、こちらの都合で奪わない。
 */
function onClick(event) {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const a = event.target.closest("a[href]");
  if (!a || a.target || a.hasAttribute("download")) return;

  const url = new URL(a.href, location.href);
  if (!internal(url)) return;
  // ページ内の飛び先(`#…`)は**ブラウザに任せる**。差し替えてしまうと
  // 飛び先まで巻き上げる動きが消え、押しても何も起きないように見える
  if (url.hash && url.pathname === location.pathname
      && url.search === location.search) return;
  if (url.href === location.href) { event.preventDefault(); return; }

  event.preventDefault();
  // 押したことを**先に**見せる。応答を待ってから印を動かすと、
  // 速いときでも「効かなかったのか」と二度押しされる
  markRail(url.pathname);
  go(url.href);
}

/** レールの現在地を先に動かす。中身が来たらサーバの印で上書きされる。 */
function markRail(pathname) {
  for (const link of document.querySelectorAll(".rail a")) {
    const same = new URL(link.href, location.href).pathname === pathname;
    if (same) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

/**
 * 行き先を取ってきて差し替える。
 *
 * @param {string} href    行き先
 * @param {boolean} push   履歴に積むか(戻る/進むのときは積まない)
 */
export async function go(href, push = true) {
  const mine = ++seq;
  const main = document.querySelector("main#main");
  if (main) main.setAttribute("aria-busy", "true");

  let html;
  let landed = href;
  try {
    const res = await fetch(href, { cache: "no-store" });
    // 落ちた・トークンが切れた等。**普通の遷移に任せる**と、サーバが
    // 用意している案内(401の文言や起動待機画面)がそのまま出る
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    // 転送されていたら、着いた先を履歴に積む
    landed = res.url || href;
    html = await res.text();
  } catch (err) {
    location.href = href;
    return;
  }
  if (mine !== seq) return;             // もっと新しい移動が始まっている

  const next = new DOMParser().parseFromString(html, "text/html");
  if (!next.querySelector("main#main")) { location.href = href; return; }

  // ここから先は差し替え。**前の画面のタイマーと listener を先に切る**
  page.abort();
  page = new AbortController();

  if (push) history.pushState({ nav: 1 }, "", landed);
  document.title = next.title;
  swapStyles(next);
  for (const sel of SWAP) swap(sel, next);
  afterSwap();
  runScripts(next);
  focusMain();
  mountViews();
}

/**
 * 画面のモジュールを繋げなかったことを、画面に出す。
 *
 * ここが転ぶと**そのページのボタンが全部効かなくなる**のに、見た目は
 * ふつうに出ています。黙っていると「押しても何も起きない」としか
 * 分からないので、理由と次にやることを本文の頭に出します。
 */
function failed(src, err) {
  const main = document.querySelector("main#main");
  if (!main || main.querySelector(".msg--wire")) return;
  const box = document.createElement("div");
  box.className = "msg msg--error msg--wire";
  box.setAttribute("role", "alert");
  box.textContent =
    "この画面の操作を読み込めませんでした。ボタンが効きません。"
    + "画面を再読込してください(Ctrl+Shift+R)。直らないときは"
    + `管理者へ: ${src} / ${err && err.message ? err.message : err}`;
  main.prepend(box);

  // 面(タブ)を全部開く。切り替えるのは JS なので、転んだままだと
  // 開いていない面へは**二度と辿り着けません**(版の印を確かめる
  // 「この端末」の面も含めて)。読みにくくても、読めないよりよい
  for (const panel of document.querySelectorAll(".tabpanel[hidden]")) {
    panel.hidden = false;
  }
}

/**
 * いまの画面をサーバから取り直して描き直す。
 *
 * `location.reload()` の代わり。**外枠を作り直さない**ので、出したばかり
 * のトーストも接続断の帯も残る ── 「保存しました」を出した直後に
 * 画面ごと作り直すと、その文言まで消えてしまう。
 */
export function refresh() {
  return go(location.href, false);
}

/** 画面ごとの `<style>`。base.html は head に `<style>` を持たない。 */
function swapStyles(next) {
  for (const old of document.head.querySelectorAll("style")) old.remove();
  for (const style of next.head.querySelectorAll("style")) {
    document.head.appendChild(document.importNode(style, true));
  }
}

function swap(selector, next) {
  const here = document.querySelector(selector);
  const there = next.querySelector(selector);
  if (!here) return;
  // 別の文書から持ってくるので `importNode`。`cloneNode` だけだと
  // 持ち主の文書が違うまま入り、古いブラウザで例外になる
  if (there) here.replaceWith(document.importNode(there, true));
  else here.remove();
}

/**
 * 画面ごとの `<script>` を入れ直す。
 *
 * ここで入れるのは**データだけ**(`<script type="application/json">` の
 * 塊など)。画面の中身を動かすモジュールは `mountViews()` が受け持つ。
 *
 * `innerHTML` で入れた `<script>` は動かない決まりなので、要素を作り直して
 * 入れる。
 */
function runScripts(next) {
  const box = document.querySelector(SCRIPT_BOX);
  const source = next.querySelector(SCRIPT_BOX);
  if (!box) return;
  box.replaceChildren();
  if (!source) return;
  for (const old of source.querySelectorAll("script")) {
    const script = document.createElement("script");
    for (const { name, value } of old.attributes) script.setAttribute(name, value);
    script.textContent = old.textContent;
    box.appendChild(script);
  }
}

/**
 * 画面ごとのモジュールを動かす。
 *
 * 【なぜ `<script src>` では駄目なのか】
 * **ES モジュールは一度読んだら二度と実行されない。** 同じ `src` の
 * `<script type="module">` をもう一度入れても、ブラウザは「もう読んだ」
 * と判断して**中身を走らせない**。差し替えで `<main>` は作り直されて
 * いるので、モジュールが最初の一度で付けた listener は一緒に消えている
 * ── つまり **2 度目にその画面へ来ると、ボタンが全部死ぬ。**
 * (管理者モードにできない・参照パスが保存できない・入力制限が効かない
 *  のは、どれもこれが原因だった)
 *
 * そこで `src` は使わず、`data-view` に道だけ書いておき、ここで
 * `import()` してから **`start()` を毎回呼ぶ**。`import()` が返すのは
 * 使い回しの同じモジュールだが、`start()` を呼ぶのはこちらなので、
 * 何度戻ってきても新しい DOM に繋ぎ直せる。
 *
 * だから各 view は次の約束を守る:
 *
 *   ・`export function start()` を持ち、**要素は毎回引き直す**
 *   ・`document` / `window` に付ける listener とタイマーは
 *     `pageSignal()` / `onLeave()` に繋ぐ(画面を出たら切れる)
 */
export async function mountViews() {
  const box = document.querySelector(SCRIPT_BOX);
  if (!box) return;
  const mine = seq;
  for (const tag of box.querySelectorAll("[data-view]")) {
    try {
      const mod = await import(tag.dataset.view);
      // 読んでいるあいだに次の画面へ移っていたら、繋がない
      if (mine !== seq) return;
      if (typeof mod.start === "function") mod.start();
    } catch (err) {
      // 1つ転んでも残りは繋ぐ。**画面ごと止めない**
      console.error("[nav] 画面のモジュールを動かせません:", tag.dataset.view, err);
      // **黙って死なせない。** ここが転ぶとその画面のボタンが全部
      // 効かなくなるが、見た目は正常なままなので「壊れている」と
      // 気づけない ── 画面に出して、次にやることまで書く
      failed(tag.dataset.view, err);
    }
  }
}

/**
 * 差し替えたあとの焦点。
 *
 * `autofocus` は後から入れた要素には効かないので、ここで当てる。
 * 無ければ `<main>` 自身へ ── 読み上げに「新しい中身になった」ことが
 * 伝わり、キーボードの続きも本文の先頭からになる。
 */
function focusMain() {
  const main = document.querySelector("main#main");
  if (!main) return;
  main.removeAttribute("aria-busy");
  main.scrollTop = 0;
  window.scrollTo(0, 0);
  const wanted = main.querySelector("[autofocus]");
  if (wanted) { wanted.focus(); return; }
  main.tabIndex = -1;
  main.focus({ preventScroll: true });
}

/**
 * 差し替えを始める。History API が無ければ何もしない(普通の遷移)。
 *
 * @param {{onSwap?: () => void}} hooks
 *   `onSwap` … リボンなど**外枠を繋ぎ直す**係。app.js が渡す。
 *   ここから app.js を呼ばないのは、輪になった読み込みを作らないため。
 */
export function start(hooks = {}) {
  if (!window.history || !window.DOMParser) return;
  if (hooks.onSwap) afterSwap = hooks.onSwap;
  document.addEventListener("click", onClick);
  // 戻る/進む。履歴に積んだのはこちらなので、同じ道で戻す
  window.addEventListener("popstate", () => {
    markRail(location.pathname);
    go(location.href, false);
  });
}
