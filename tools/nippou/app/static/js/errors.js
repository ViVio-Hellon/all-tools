/*
  errors.js — 画面(ブラウザ)で起きたエラーを、サーバの記録へ届ける

    エラー等の後追いが現状できないと感じている

  画面の中で起きたエラー(JS の例外・握られなかった失敗)は、**押した人の
  画面の奥でしか見えません。** 押しても何も起きない・表が出ない、とだけ
  言われても、記録のどこにも手がかりがありませんでした。

  ここがやること:
    1. 直前に押したもの・選んだものを**15件だけ**覚えておく(パンくず)
       ── なぜなぜの「その前に何をしていたか」
    2. エラーが起きたら、パンくずを添えて `/api/log/client` へ送る
    3. 返ってきた**番号**を画面に出す(サーバのエラーと同じ扱い)

  **送る中身は打った値ではありません。** 押したボタンの名前・選んだ欄の
  名前だけです(値は日報そのものなので、記録には要りません)。
*/

import { background } from "./api.js";
import { toast } from "./toast.js";

const KEEP = 15;
const crumbs = [];

/** 1回の読み込みで送る数の上限。壊れた画面が毎秒送るのを止める */
const MAX_SEND = 20;
let sent = 0;
let shown = false;
const recent = new Map();

/** 明らかに害の無いもの(ブラウザ自身の都合) */
const HARMLESS = [/ResizeObserver loop/i, /^Script error\.?$/i];

function now() {
  const d = new Date();
  return d.toTimeString().slice(0, 8);
}

function nameOf(el) {
  if (!el) return "";
  const text = (el.getAttribute("aria-label") || el.textContent || el.value || "")
    .replace(/\s+/g, " ").trim().slice(0, 30);
  const id = el.id ? `#${el.id}` : "";
  return [text, id].filter(Boolean).join(" ");
}

function remember(what) {
  crumbs.push(`${now()} ${what}`);
  if (crumbs.length > KEEP) crumbs.shift();
}

/** いまのパンくず(写し)。 */
export function trail() {
  return crumbs.slice();
}

function watch() {
  document.addEventListener("click", (event) => {
    const el = event.target.closest?.("button, a, [role=tab], summary, input[type=checkbox], input[type=radio]");
    if (el) remember(`押した: ${nameOf(el)}`);
  }, true);
  document.addEventListener("change", (event) => {
    const el = event.target;
    if (el && el.matches?.("select")) {
      // 選んだ欄の名前だけ。**値は送らない**
      remember(`選んだ: ${el.id ? `#${el.id}` : el.name || "選択欄"}`);
    }
  }, true);
}

async function report(kind, message, where, stack) {
  if (sent >= MAX_SEND) return;
  if (HARMLESS.some((re) => re.test(message))) return;
  const key = `${message}@${where}`;
  const last = recent.get(key) || 0;
  if (Date.now() - last < 10_000) return;      // 同じものは10秒に1度
  recent.set(key, Date.now());
  sent += 1;
  try {
    const body = await background.post("/api/log/client", {
      kind, message, where, stack: String(stack || "").slice(0, 6000),
      screen: location.pathname, crumbs: trail(),
      version: (window.APP && window.APP.version) || "",
    });
    if (body && body.id && !shown) {
      // **1回の読み込みで1度だけ**知らせる(続けて出すと画面が埋まる)
      shown = true;
      // 音の出来事「エラーが起きた」(鳴らすかは設定 ── `sound.js`)
      window.dispatchEvent(new CustomEvent("sound:cue", { detail: "error" }));
      toast(`画面でエラーが起きました(番号 ${body.id})。動きがおかしいときは、`
            + "この番号を管理者に伝えてください。", "warn");
    }
  } catch (err) {
    // 送れなくても何もしない(送ることでまたエラーにしない)
  }
}

export function startErrorReport() {
  watch();
  window.addEventListener("error", (event) => {
    // 読めなかった部品(<script>・<img>)は event.error を持たない
    const target = event.target;
    if (target && target !== window && (target.src || target.href)) {
      report("load", `読めなかった: ${target.src || target.href}`, "", "");
      return;
    }
    const where = event.filename
      ? `${event.filename.replace(location.origin, "")}:${event.lineno}:${event.colno}` : "";
    report("error", String(event.message || event.error || "エラー"), where,
           event.error && event.error.stack);
  }, true);
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    // サーバの断り・エラー(ApiError)は**サーバがもう残している**
    if (reason && reason.name === "ApiError") return;
    report("rejection", String((reason && reason.message) || reason || "失敗"), "",
           reason && reason.stack);
  });
}
