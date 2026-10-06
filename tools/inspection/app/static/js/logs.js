/*
  logs.js — 設定の「ログ」タブ(後追い・なぜなぜ分析)

  ・ログの保存先(共有フォルダを指定すれば全ラインのログが1か所に集まる)・保存日数
  ・問い合わせ番号で探す → その出来事の「なぜ」の段と、そのときの状況、直前の操作
  ・この端末の最近のエラー・断り

  記録の仕組みはサーバ(core/event_log.py)。ここは送って、返ってきたものを描くだけ。
*/

import { api } from "./api.js";
import * as desktop from "./desktop.js";
import { toast, toastError } from "./toast.js";

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function why(id, message) {
  $(id).textContent = message || "";
  $(id).hidden = !message;
}

/* 記録の名前を現場の言葉に */
const OPS = {
  "preview": "プレビュー", "print": "印刷の受付", "print.start": "印刷の開始", "print.item": "印刷(1件)",
  "print.end": "印刷の結果", "print.cancel": "印刷の中止", "print.status": "印刷の進み具合",
  "scan": "点検表フォルダの確認", "inventory": "一覧", "inventory.refresh": "一覧の読み直し",
  "excel.job": "Excel の処理", "excel.open": "Excel の起動", "excel.close": "Excel を閉じた",
  "excel.warm": "Excel の先回り起動", "startup": "アプリの起動", "shutdown": "アプリの終了",
  "shutdown.request": "終了の要求", "cleanup": "前回の残りの片付け",
  "settings": "設定", "settings.folder": "点検表フォルダの変更", "settings.logs": "ログの設定",
  "settings.admin_password": "管理者パスワード", "settings.distribution": "配布設定",
  "client.js_error": "画面のエラー", "client.unhandled": "画面のエラー", "client.offline": "接続が切れていた",
  "client.api_error": "画面の通信エラー",
};
const RESULTS = { ok: "OK", ng: "NG", refused: "断り", info: "記録" };

/* 「そのときの状況」に出す項目の名前(出す順) */
const FIELDS = [
  ["target", "対象"], ["file", "ファイル"], ["folder", "フォルダ"], ["printer", "プリンター"],
  ["sheet", "シート"], ["range", "範囲"], ["copies", "部数"], ["index", "何件目"], ["total", "件数"],
  ["succeeded", "成功"], ["failed", "失敗"], ["state", "状態"], ["mode", "Excel の処理"],
  ["finish_code", "終了コード"], ["timed_out", "時間切れ"], ["reused_excel", "起動済みの Excel"],
  ["excel_pid", "Excel の PID"], ["excel_busy_with", "Excel が使用中だった処理"], ["elapsed_ms", "かかった時間(ms)"],
  ["cached", "控えの画像"], ["force", "作り直し"], ["os_error", "OS のエラー"], ["status", "HTTP"],
  ["method", "要求"], ["path", "宛先"], ["reason", "きっかけ"], ["by", "誰が"], ["action", "操作"],
  ["page", "画面"], ["related_ref", "関連の番号"], ["found", "検出"], ["listed", "表示"],
  ["port", "ポート"], ["skipped_ports", "使えなかったポート"], ["log_dir", "ログ"], ["log_fallback", "ログの退避"],
  ["python", "Python"], ["store_python", "Store 版 Python"], ["excel", "Excel 連携"], ["seconds", "起動の秒数"],
  ["uptime_sec", "稼働秒数"], ["changed", "変えた項目"], ["kept", "そのままの項目"],
  ["before", "前"], ["after", "後"], ["before_dir", "前の保存先"], ["after_dir", "後の保存先"],
  ["read_errors", "読めなかったもの"], ["results", "Excel の返答"], ["worker_log", "処理プログラムの記録"],
  ["targets", "対象の一覧"], ["user_agent", "ブラウザ"],
];
const SKIP = new Set(["ts", "ref", "pc", "user", "ver", "pid", "screen", "op", "result", "code",
                      "message", "detail", "why", "job", "checks"]);

function opName(op) { return OPS[op] || op; }
function when(ts) { return (ts || "").replace("T", " ").slice(0, 19); }
function show(value) {
  if (value === true) return "はい";
  if (value === false) return "いいえ";
  if (Array.isArray(value) || (value && typeof value === "object")) return JSON.stringify(value, null, 1);
  return String(value);
}

let last = null;     // いま出している出来事(文にして写す用)

/* ---------------------------------------------------------------- 状態 */
function render(data) {
  const s = data.logs;
  const state = s.fallback_reason ? ["退避中", "warn"] : s.is_local ? ["この PC", "ok"] : ["指定のフォルダ", "ok"];
  $("log-state").textContent = state[0];
  $("log-state").className = `st st--${state[1]}`;
  why("log-fallback", s.fallback_reason
    ? `指定のフォルダ ${s.requested} に書けないため、この PC に退避して書いています(${s.fallback_reason})。戻れば自動で戻ります。`
    : "");
  $("log-dir").textContent = s.dir;
  $("log-file").textContent = s.file.split(/[\\/]/).pop();
  $("log-events").textContent = s.events_file.split(/[\\/]/).pop();
  $("log-keep-now").textContent = String(s.keep_days);
  if (document.activeElement !== $("log-dir-input")) $("log-dir-input").value = s.requested || "";
  if (document.activeElement !== $("log-keep")) $("log-keep").value = String(s.keep_days);
  $("log-reset").disabled = !s.requested;
  $("log-local-note").textContent = s.visible_local_dir && s.visible_local_dir !== s.local_dir
    ? `この PC のログは、エクスプローラーでは ${s.visible_local_dir} に見えます(Microsoft Store 版の Python のため)。`
    : `この PC のログ: ${s.local_dir}`;
  $("log-days").textContent = String(data.recent_days || 7);
  renderRecent(data.recent || []);
}

function renderRecent(rows) {
  const list = $("log-recent");
  if (!rows.length) {
    list.replaceChildren(el("li", "empty", "この期間のエラー・断りはありません。"));
    return;
  }
  list.replaceChildren(...rows.map((e) => {
    const li = el("li");
    const b = el("button");
    b.type = "button";
    b.title = "なぜなぜを出す";
    const m = el("span", "m");
    m.append(el("span", `res res--${e.result}`, RESULTS[e.result] || e.result), el("b", null, opName(e.op)),
             document.createTextNode(e.message || e.code || ""));
    b.append(el("span", "t", when(e.ts)), el("span", "r", e.ref), m);
    b.addEventListener("click", () => find(e.ref));
    li.appendChild(b);
    return li;
  }));
}

export async function load() {
  try { render(await api.get("/api/logs")); } catch (err) { toastError(err); }
}

/* ---------------------------------------------------------------- なぜなぜ */
async function find(ref) {
  why("log-find-why", "");
  ref = (ref || $("log-ref").value || "").trim().toUpperCase();
  if (!ref) { why("log-find-why", "問い合わせ番号を入力してください。"); return; }
  $("log-ref").value = ref;
  try {
    const data = await api.get(`/api/logs/find?ref=${encodeURIComponent(ref)}`);
    renderDetail(data);
  } catch (err) {
    why("log-find-why", err.message + (err.detail ? `\n${err.detail}` : ""));
  }
}

function renderDetail(data) {
  const events = data.events || [];
  // 中心にするのは、いちばん重い結果(NG → 断り → それ以外)
  const main = events.find((e) => e.result === "ng") || events.find((e) => e.result === "refused") || events[0];
  last = { ...data, main };
  $("log-detail-ref").textContent = `${data.ref}`;
  const box = $("log-detail");
  box.replaceChildren();

  box.appendChild(el("p", "why",
    `${when(main.ts)} ・ ${main.pc} / ${main.user} ・ VER${main.ver} ・ ${opName(main.op)}`));

  // なぜ の段。記録から言えるところまでを埋め、その先は空けておく(現場で考える段)
  const steps = main.why && main.why.length ? main.why : [main.message || main.code || "(記録なし)"];
  const ol = el("ol", "why5");
  steps.forEach((text, i) => {
    const li = el("li");
    li.append(el("span", "k", i === 0 ? "現象" : `なぜ${i}`), el("span", null, text));
    ol.appendChild(li);
  });
  for (let i = steps.length; i <= 5; i++) {
    const li = el("li", "todo");
    li.append(el("span", "k", `なぜ${i}`),
              el("span", null, i === steps.length ? "← ここから先は、下の「そのときの状況」と「直前の操作」から考える" : ""));
    ol.appendChild(li);
  }
  box.append(el("div", "logsec", "なぜなぜ(記録から言えるところまで)"), ol);

  // 次の「なぜ」を考えるときに確かめること(エラーの種類ごと。app/why_hints.py)
  if (main.checks && main.checks.length) {
    const ul = el("ul", "logchecks");
    for (const c of main.checks) ul.appendChild(el("li", null, c));
    box.append(el("div", "logsec", "確かめること(次の「なぜ」の手がかり)"), ul);
  }

  // そのときの状況
  const ctx = el("dl", "kv logctx");
  const shown = new Set();
  const addRow = (label, value) => {
    const dd = el("dd");
    const text = show(value);
    if (text.includes("\n") || text.length > 160) dd.appendChild(el("pre", null, text));
    else dd.textContent = text;
    ctx.append(el("dt", null, label), dd);
  };
  if (main.code) addRow("エラーの種類", main.code);
  if (main.detail) addRow("詳しい原因", main.detail);
  for (const [key, label] of FIELDS) {
    if (main[key] !== undefined && main[key] !== null && main[key] !== "") { addRow(label, main[key]); shown.add(key); }
  }
  for (const [key, value] of Object.entries(main)) {
    if (!SKIP.has(key) && !shown.has(key)) addRow(key, value);
  }
  box.append(el("div", "logsec", "そのときの状況"), ctx);

  // 同じ番号の記録(印刷なら 開始 → 1件ずつ → 結果)と、直前の操作
  const tl = (rows, cls) => {
    const ul = el("ul", "logtl");
    for (const e of rows) {
      const li = el("li", e === main ? "this" : cls || "");
      const text = el("span");
      text.append(el("span", `res res--${e.result}`, RESULTS[e.result] || e.result),
                  document.createTextNode(`${opName(e.op)} ${e.target ? `「${e.target}」 ` : ""}${e.message || e.code || ""}`));
      li.append(el("span", "t", when(e.ts).slice(11)), text);
      ul.appendChild(li);
    }
    return ul;
  };
  if (events.length > 1) box.append(el("div", "logsec", `同じ番号の記録(${events.length} 件)`), tl(events));
  box.append(el("div", "logsec", "直前に起きていたこと(同じアプリ・古い順)"),
             (data.before || []).length ? tl(data.before) : el("p", "why", "(この前の記録はありません)"));

  $("log-detail-card").hidden = false;
  $("log-detail-card").scrollIntoView({ block: "nearest" });
}

/** なぜなぜの表に貼れる文にする(メール・Teams に貼って共有する)。 */
function asText() {
  if (!last) return "";
  const m = last.main;
  const lines = [`【問い合わせ番号】${last.ref}`, `【日時】${when(m.ts)}`,
                 `【端末】${m.pc} / ${m.user} / VER${m.ver}`, `【操作】${opName(m.op)}`];
  (m.why && m.why.length ? m.why : [m.message || m.code]).forEach((t, i) => lines.push(`${i === 0 ? "【現象】" : `【なぜ${i}】`}${t}`));
  lines.push("【なぜ(続き)】", "");
  if (m.checks && m.checks.length) {
    lines.push("【確かめること】");
    for (const c of m.checks) lines.push(`  ・${c}`);
    lines.push("");
  }
  lines.push("【そのときの状況】");
  for (const [key, label] of FIELDS) if (m[key] !== undefined && m[key] !== "") lines.push(`  ${label}: ${show(m[key])}`);
  if (m.detail) lines.push(`  詳しい原因: ${m.detail}`);
  lines.push("", "【直前に起きていたこと】");
  for (const e of last.before || []) lines.push(`  ${when(e.ts).slice(11)} ${RESULTS[e.result] || e.result} ${opName(e.op)} ${e.message || ""}`);
  return lines.join("\n");
}

/* ---------------------------------------------------------------- 保存先 */
async function save(body) {
  why("log-why", "");
  try {
    const data = await api.post("/api/settings/logs", body);
    render(data);
    toast(data.message || "保存しました", "ok");
  } catch (err) {
    why("log-why", err.message + (err.detail ? `\n${err.detail}` : ""));
  }
}

export function init() {
  $("tab-logs").addEventListener("click", load);
  $("log-reload").addEventListener("click", load);
  $("log-save").addEventListener("click", () => {
    const folder = $("log-dir-input").value.trim();
    const body = { keep_days: Number($("log-keep").value) };
    if (folder) body.log_dir = folder; else body.reset_log_dir = true;
    save(body);
  });
  $("log-reset").addEventListener("click", () => save({ reset_log_dir: true }));
  // デスクトップ版: Windows の「フォルダーの選択」窓
  $("log-pick").addEventListener("click", async () => {
    try {
      const picked = await desktop.pickFolder($("log-dir-input").value.trim() || $("log-dir").textContent,
                                              "ログの保存先を選ぶ");
      if (picked) $("log-dir-input").value = picked;
    } catch (err) { why("log-why", String(err)); }
  });
  $("log-open").addEventListener("click", async () => {
    try { await api.post("/api/logs/open-folder"); } catch (err) { why("log-why", err.message + (err.detail ? `\n${err.detail}` : "")); }
  });
  $("log-find").addEventListener("click", () => find());
  $("log-ref").addEventListener("keydown", (ev) => { if (ev.key === "Enter") find(); });
  $("log-copy").addEventListener("click", async () => {
    const text = asText();
    try { await navigator.clipboard.writeText(text); toast("写しました。メールや Teams に貼れます。", "ok"); }
    catch { window.prompt("この文を写してください", text); }
  });
}
