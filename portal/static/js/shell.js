/*
  shell.js — 大きなタブの画面

  ・タブ表示権限で決まったツールのタブと「大設定」を並べる(`/api/tabs`)
  ・選んだタブのツールの画面(iframe)を出す。ほかのタブの画面も**消さずに残す**
    (打ちかけ・開いている画面がそのまま。隠すだけ)
  ・最初のタブが出たら、ほかのタブのツールも順に裏で起こしておく(切り替えを待たせない)

  デスクトップ版(統合ツールの窓)では、さらに:
  ・各ツールの画面からの頼みごと(別窓・保存・フォルダの選択・立て直し)を外枠(Rust)へ
    取り次ぐ(`postMessage` → `__TAURI_INTERNALS__.invoke`)。**送り元の枠と宛先を確かめる**
  ・各ツールの Python の状態(起動中・停止)をタブの印に出す
  ・「終了」は外枠に頼む(全ツールに訊いてから、まとめて1つの確認)

  ブラウザ版(予備)では、各ツールのブラウザ版を入口の Python が起こし、その宛先を枠に出す。
*/
import { api } from "./api.js";
import { toast, toastError } from "./toast.js";
import * as settingsView from "./settings.js";

const S = window.SHELL || {};
const desktop = S.edition === "desktop";
const internals = window.__TAURI_INTERNALS__;
const TOOLS = new Map((S.tools || []).map((t) => [t.id, t]));
const SETTINGS = "settings";
const MARK = { nippou: "日", kanban: "看", calendar: "暦", inspection: "点", [SETTINGS]: "設" };
const LAST_KEY = "alltools.lastTab";
const PRELOAD_GAP_MS = 1500;

// ------------------------------------------------------------------
// 記録(画面の中で起きたことは、入口の Python のログへ送らないと後から追えない)
// ------------------------------------------------------------------
function report(level, message, where = "") {
  api.post("/api/log/client", { level, message: String(message || "").slice(0, 2000), where }).catch(() => {});
}
window.addEventListener("error", (event) => {
  report("error", event.message, `${event.filename || ""}:${event.lineno || 0}`);
});
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  report("error", (reason && reason.message) || String(reason), "promise");
});

const tabsBar = document.getElementById("bigtabs");
const stage = document.getElementById("stage");

/** id → { panel, iframe, button, url, loaded } */
const frames = new Map();
let order = [];           // いま出しているタブの並び(大設定を含む)
let current = "";
let decision = null;

// ------------------------------------------------------------------
// 外枠(Rust)への口
// ------------------------------------------------------------------
function invoke(cmd, args, options) {
  if (!internals) return Promise.reject(new Error("外枠がありません"));
  return internals.invoke(cmd, args, options);
}

function originOf(id) {
  const tool = TOOLS.get(id);
  return tool ? tool.origin : "";
}

// ------------------------------------------------------------------
// タブ
// ------------------------------------------------------------------
function tabButton(id, title, sub, version = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "bigtab" + (id === SETTINGS ? " bigtab--settings" : "");
  button.setAttribute("role", "tab");
  button.dataset.tab = id;
  button.id = `tab-${id}`;
  button.setAttribute("aria-controls", `panel-${id}`);
  button.setAttribute("aria-selected", "false");
  button.tabIndex = -1;
  button.style.setProperty("--c", `var(--tool-${id})`);
  const mark = document.createElement("span");
  mark.className = "bigtab__mark";
  mark.textContent = MARK[id] || title.slice(0, 1);
  mark.setAttribute("aria-hidden", "true");
  // 1段目: 短い名前と版。2段目: 正式名
  const head = document.createElement("span");
  head.className = "bigtab__head";
  const name = document.createElement("span");
  name.className = "bigtab__title";
  name.textContent = title;
  head.append(name);
  if (version) {
    const ver = document.createElement("span");
    ver.className = "bigtab__ver";
    ver.textContent = `VER${version}`;
    head.append(ver);
    button.title = `${sub} VER${version}`;
  }
  const small = document.createElement("span");
  small.className = "bigtab__sub";
  small.textContent = sub;
  const dot = document.createElement("span");
  dot.className = "bigtab__dot";
  dot.setAttribute("aria-hidden", "true");
  button.append(mark, head, small, dot);
  button.addEventListener("click", () => select(id));
  return button;
}

function renderTabs(ids) {
  tabsBar.replaceChildren();
  order = [...ids, SETTINGS];
  for (const id of ids) {
    const tool = TOOLS.get(id);
    tabsBar.append(tabButton(id, tool.title, tool.name, tool.version));
  }
  tabsBar.append(tabButton(SETTINGS, "大設定", "タブ表示権限"));
}

// 矢印キーで隣のタブへ(タブの帯にいるとき)
tabsBar.addEventListener("keydown", (event) => {
  const i = order.indexOf(current);
  let next = "";
  if (event.key === "ArrowRight") next = order[(i + 1) % order.length];
  else if (event.key === "ArrowLeft") next = order[(i - 1 + order.length) % order.length];
  else if (event.key === "Home") next = order[0];
  else if (event.key === "End") next = order[order.length - 1];
  if (!next) return;
  event.preventDefault();
  select(next);
  document.getElementById(`tab-${next}`)?.focus();
});

// ------------------------------------------------------------------
// 画面(iframe)
// ------------------------------------------------------------------
async function toolUrl(id) {
  if (desktop) return `${originOf(id)}/`;
  // ブラウザ版: 入口の Python がそのツールのブラウザ版を起こし、宛先を返す
  const body = await api.post(`/api/tools/${encodeURIComponent(id)}/open`, {});
  return body.url;
}

function panelFor(id) {
  let entry = frames.get(id);
  if (entry) return entry;
  const panel = document.createElement("section");
  panel.className = "panel";
  panel.id = `panel-${id}`;
  panel.dataset.panel = id;
  panel.setAttribute("role", "tabpanel");
  panel.setAttribute("aria-labelledby", `tab-${id}`);
  panel.inert = true;
  stage.append(panel);
  entry = { panel, iframe: null, url: "", loaded: false, starting: null };
  frames.set(id, entry);
  return entry;
}

/** そのツールの画面を用意する(まだなら)。起こすのはここ。 */
function ensureFrame(id) {
  const entry = panelFor(id);
  if (entry.iframe || entry.starting) return entry.starting || Promise.resolve(entry);
  entry.starting = (async () => {
    try {
      entry.url = await toolUrl(id);
    } catch (err) {
      showProblem(entry, TOOLS.get(id), err);
      return entry;
    } finally {
      entry.starting = null;
    }
    const iframe = document.createElement("iframe");
    iframe.title = TOOLS.get(id).name;
    // クリップボード(なぜなぜの写し・GW の写し)・音・全画面を、ツールの画面に許す
    iframe.allow = "clipboard-read; clipboard-write; autoplay; fullscreen";
    iframe.src = entry.url;
    iframe.addEventListener("load", () => {
      entry.loaded = true;
      // ブラウザ版は台本が入らないので、ここで残す(デスクトップ版はツールの画面が知らせてくる)
      if (!desktop && !entry.reported) {
        entry.reported = true;
        // 宛先は合言葉(t=)を落として残す
        report("info", `${TOOLS.get(id).title} の画面が出ました`, String(entry.url).split("?")[0]);
      }
    });
    entry.iframe = iframe;
    entry.panel.replaceChildren(iframe);
    return entry;
  })();
  return entry.starting;
}

/** 枠の代わりに知らせを出す(開けなかった・終了した)。「もう一度開く」で起こし直す */
function showNote(entry, title, text) {
  const id = entry.panel.dataset.panel;
  entry.iframe = null;
  entry.loaded = false;
  entry.reported = false;
  entry.reports = 0;
  entry.lastTitle = "";
  entry.missing = 0;
  const box = document.createElement("div");
  box.className = "panel--note";
  box.style.cssText = "position:absolute;inset:0";
  const card = document.createElement("div");
  card.className = "card note-card";
  const h = document.createElement("h1");
  h.textContent = title;
  const p = document.createElement("p");
  p.textContent = text;
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "btn btn--primary";
  retry.textContent = "もう一度開く";
  retry.addEventListener("click", () => {
    entry.panel.replaceChildren();
    ensureFrame(id).then(() => { if (current === id) showPanel(id); });
  });
  card.append(h, p, retry);
  box.append(card);
  entry.panel.replaceChildren(box);
}

function showProblem(entry, tool, err) {
  showNote(entry, `${tool ? tool.name : ""}を開けませんでした`, (err && err.message) || String(err));
}

function showPanel(id) {
  for (const node of stage.querySelectorAll(".panel")) {
    const on = node.dataset.panel === id;
    node.classList.toggle("is-shown", on);
    node.inert = !on;
    if (node.dataset.panel === "starting" || node.dataset.panel === "none") node.hidden = !on;
  }
}

function select(id) {
  if (!order.includes(id)) return;
  current = id;
  if (manualBox && !manualBox.hidden) openManual(id);
  for (const button of tabsBar.querySelectorAll(".bigtab")) {
    const on = button.dataset.tab === id;
    button.setAttribute("aria-selected", on ? "true" : "false");
    button.tabIndex = on ? 0 : -1;
  }
  if (id === SETTINGS) {
    document.getElementById("panel-settings").hidden = false;
    showPanel(SETTINGS);
    settingsView.opened();
  } else {
    ensureFrame(id).then(() => {
      if (current !== id) return;
      showPanel(id);
      // 打てるように、選んだツールの画面へ入る
      try { frames.get(id)?.iframe?.contentWindow?.focus(); } catch (err) { /* 別の宛先 */ }
    });
    showPanel(id);
  }
  const title = id === SETTINGS ? "大設定" : TOOLS.get(id)?.title;
  document.title = `${S.name || "統合ツール"} — ${title}`;
  try { localStorage.setItem(LAST_KEY, id); } catch (err) { /* 残せなくても困らない */ }
}

/** 最初のタブが開いたら、ほかのツールも順に裏で起こしておく */
async function preloadOthers(first) {
  const entry = frames.get(first);
  const started = Date.now();
  while (entry && !entry.loaded && Date.now() - started < 60000) {
    await new Promise((r) => setTimeout(r, 300));
  }
  for (const id of order) {
    if (id === SETTINGS || frames.get(id)?.iframe) continue;
    await new Promise((r) => setTimeout(r, PRELOAD_GAP_MS));
    ensureFrame(id);
  }
}

function firstTab(body) {
  const ids = body.tabs.map((t) => t.id);
  let last = "";
  try { last = localStorage.getItem(LAST_KEY) || ""; } catch (err) { /* 無ければ無いで */ }
  if (body.default_tab && ids.includes(body.default_tab)) return body.default_tab;
  if (last && (ids.includes(last) || last === SETTINGS)) return last;
  return ids[0] || SETTINGS;
}

async function start() {
  let body;
  try {
    body = await api.get("/api/tabs");
  } catch (err) {
    toastError(err);
    body = { tabs: [], decision: { reason: (err && err.message) || "" } };
  }
  decision = body.decision;
  const ids = body.tabs.map((t) => t.id).filter((id) => TOOLS.has(id));
  renderTabs(ids);
  document.getElementById("panel-starting").hidden = true;
  // ツールのタブが1つも無い端末は、大設定を開く(登録のしかたと、この端末の ID・PC名が出る)
  const first = ids.length ? firstTab(body) : SETTINGS;
  report("info", `大きなタブの画面がつながりました(${desktop ? "デスクトップ版" : "ブラウザ版"}): `
    + `${ids.join(",") || "ツールのタブなし"} / ${(decision && decision.source) || ""}`);
  select(first);
  if (ids.length) preloadOthers(first === SETTINGS ? ids[0] : first);
  setInterval(followTabs, 60000);
}

/** 外れたと知らせたタブ(知らせるのは1度だけ) */
const noticed = new Set();

/** タブ表示権限が変わったか(1分ごと)。**増えたタブはその場で足す。** 減ったタブは次の起動で消す */
async function followTabs() {
  let body;
  try { body = await api.get("/api/tabs"); } catch (err) { return; }
  const ids = body.tabs.map((t) => t.id).filter((id) => TOOLS.has(id));
  const shown = order.filter((id) => id !== SETTINGS);
  const added = ids.filter((id) => !shown.includes(id));
  const removed = shown.filter((id) => !ids.includes(id));
  if (added.length) {
    renderTabs([...shown, ...added].sort((a, b) => [...TOOLS.keys()].indexOf(a) - [...TOOLS.keys()].indexOf(b)));
    select(current);
    toast(`タブが増えました: ${added.map((id) => TOOLS.get(id).title).join("、")}`);
  }
  // 減ったタブは、打ちかけ・途中の処理があるかもしれないので、その場では消さない
  const fresh = removed.filter((id) => !noticed.has(id));
  for (const id of removed) {
    noticed.add(id);
    document.getElementById(`tab-${id}`)?.setAttribute("title", "タブ表示権限から外れました。次に開いたときに消えます");
  }
  if (fresh.length) {
    toast(`${fresh.map((id) => TOOLS.get(id).title).join("、")} のタブは、この端末では出さない設定になりました。`
      + "次に統合ツールを開いたときに消えます。", "ok", 9000);
  }
  settingsView.decisionChanged(body.decision);
}

// ------------------------------------------------------------------
// ツールの画面からの頼みごと(デスクトップ版)
// ------------------------------------------------------------------
function frameOf(source) {
  for (const [id, entry] of frames) {
    if (entry.iframe && entry.iframe.contentWindow === source) return id;
  }
  return "";
}

window.addEventListener("message", async (event) => {
  const data = event.data;
  if (!data || typeof data !== "object" || typeof data.type !== "string") return;
  if (!data.type.startsWith("alltools:")) return;
  const id = frameOf(event.source);
  // **送り元を確かめる**: 自分が出した枠からで、その枠のツールの宛先であること
  if (!id || event.origin !== originOf(id)) return;
  if (data.type === "alltools:key") {
    keyFromTool(id, data);
    return;
  }
  if (data.type === "alltools:ready") {
    // ツールの画面そのものが出た(外枠の「起動しています」・理由の画面ではない)。
    // ツールの待機画面 → 本体の画面と移るので、題名が変わったときに残す(1つの枠で5回まで)
    const entry = frames.get(id);
    const title = String(data.title || "").slice(0, 80);
    if (entry && title !== entry.lastTitle && (entry.reports || 0) < 5) {
      entry.lastTitle = title;
      entry.reports = (entry.reports || 0) + 1;
      entry.reported = true;
      report("info", `${TOOLS.get(id).title} の画面が出ました: ${title}`, String(data.path || ""));
    }
    return;
  }
  if (data.type !== "alltools:invoke" || !desktop) return;
  const reply = (ok, value) => {
    try { event.source.postMessage({ type: "alltools:result", id: data.id, ok, value }, event.origin); }
    catch (err) { /* 枠が先に消えた */ }
  };
  try {
    let value;
    if (data.raw) {
      value = await invoke("tool_invoke_raw", data.raw, {
        headers: { ...(data.headers || {}), "x-alltools-tool": id, "x-alltools-cmd": String(data.cmd) },
      });
    } else {
      value = await invoke("tool_invoke", { tool: id, cmd: String(data.cmd), args: data.args || {} });
    }
    reply(true, value === undefined ? null : value);
  } catch (err) {
    reply(false, (err && err.message) || String(err));
  }
});

/** ツールの画面の中で押されたキー(タブの切り替え・読み直し) */
function keyFromTool(id, data) {
  if (data.action === "tab" && Number.isInteger(data.index)) {
    const next = order[data.index];
    if (next) select(next);
  } else if (data.action === "next") {
    select(order[(order.indexOf(current) + 1) % order.length]);
  } else if (data.action === "prev") {
    select(order[(order.indexOf(current) - 1 + order.length) % order.length]);
  } else if (data.action === "help") {
    openManual(id);
  }
}

// この画面(タブの帯)で押されたキー
document.addEventListener("keydown", (event) => {
  if (event.key === "F1") {
    event.preventDefault();
    if (manualBox.hidden) openManual(); else closeManual();
    return;
  }
  if (event.key === "Escape" && !manualBox.hidden) {
    event.preventDefault();
    closeManual();
    return;
  }
  // F5 / Ctrl+R は**いま出しているツールの画面だけ**を読み直す(全タブを読み直さない)
  if (event.key === "F5" || ((event.ctrlKey || event.metaKey) && (event.key === "r" || event.key === "R"))) {
    event.preventDefault();
    reloadTool(current);
    return;
  }
  if (event.altKey && /^[1-9]$/.test(event.key)) {
    const next = order[Number(event.key) - 1];
    if (next) { event.preventDefault(); select(next); }
  }
});

function reloadTool(id) {
  const entry = frames.get(id);
  if (!entry || !entry.iframe) return;
  entry.loaded = false;
  entry.iframe.src = entry.url;
}

// 外枠(Rust)から呼ばれる口
window.__shell = {
  /** そのツールの Python を立て直した・落ちた → そのタブだけ読み直す */
  reloadTool(id) { reloadTool(id); },
  toolLost(id) {
    reloadTool(id);
    if (id !== current) {
      toast(`${TOOLS.get(id)?.title || id} の処理が止まりました。タブを開くと理由が出ます。`, "ng", 9000);
    }
  },
  select(id) { select(id); },
};

// ------------------------------------------------------------------
// 状態の印
// ------------------------------------------------------------------
async function pollStatus() {
  if (!desktop) {
    await watchBrowserTools();
    return;
  }
  try {
    const list = await invoke("shell_status");
    for (const item of list || []) {
      const dot = document.querySelector(`#tab-${item.id} .bigtab__dot`);
      if (dot) dot.dataset.phase = item.phase;
    }
    settingsView.statusChanged(list || []);
  } catch (err) { /* 次で */ }
}

/**
 * ブラウザ版: 開いたツールのブラウザ版が動いているか(数秒ごと)。
 * そのツールの画面の「終了」で止まったら、タブに「終了しました / もう一度開く」を出す
 * (枠の中に「接続できません」を出したままにしない)。2回続けて居なければ、とする。
 */
async function watchBrowserTools() {
  const ids = [...frames.entries()].filter(([, e]) => e.iframe && e.loaded).map(([id]) => id);
  if (!ids.length) return;
  let body;
  try { body = await api.get(`/api/tools/status?ids=${encodeURIComponent(ids.join(","))}`); }
  catch (err) { return; }
  for (const item of body.tools || []) {
    const entry = frames.get(item.id);
    if (!entry || !entry.iframe) continue;
    const dot = document.querySelector(`#tab-${item.id} .bigtab__dot`);
    if (item.running) {
      entry.missing = 0;
      if (dot) dot.dataset.phase = "started";
      continue;
    }
    entry.missing = (entry.missing || 0) + 1;
    if (entry.missing < 2) continue;
    if (dot) dot.dataset.phase = "quit";
    const tool = TOOLS.get(item.id);
    report("info", `${tool.title} のブラウザ版が終わりました`);
    showNote(entry, `${tool.name}は終了しました`,
             "このツールのブラウザ版は止まっています。ほかのタブはそのまま使えます。");
  }
}

// ------------------------------------------------------------------
// 操作説明書(画面写真入り。portal/static/manual/)。いま出しているタブの説明書を、
// ツールの画面の上に重ねて開く(ツールの画面はそのまま。閉じれば元どおり)
// ------------------------------------------------------------------
const MANUALS = new Set(["portal", "nippou", "kanban", "calendar", "inspection"]);
const manualBox = document.getElementById("manual");
const manualFrame = document.getElementById("manual-frame");

function manualFor(id) {
  if (id === SETTINGS) return "portal";
  return MANUALS.has(id) ? id : "index";
}

function openManual(id = current) {
  const page = manualFor(id);
  const url = `/static/manual/${page}.html?v=${encodeURIComponent(S.version || "")}`;
  if (manualFrame.dataset.page !== page) {
    manualFrame.dataset.page = page;
    manualFrame.src = url;
  }
  manualBox.hidden = false;
  document.getElementById("help").setAttribute("aria-expanded", "true");
  document.getElementById("manual-close").focus();
}

function closeManual() {
  if (manualBox.hidden) return;
  manualBox.hidden = true;
  document.getElementById("help").setAttribute("aria-expanded", "false");
  try { frames.get(current)?.iframe?.contentWindow?.focus(); } catch (err) { /* 別の宛先 */ }
}

document.getElementById("help").addEventListener("click", () => {
  if (manualBox.hidden) openManual(); else closeManual();
});
document.getElementById("manual-close").addEventListener("click", closeManual);
document.getElementById("manual-index").addEventListener("click", () => {
  manualFrame.dataset.page = "index";
  manualFrame.src = `/static/manual/index.html?v=${encodeURIComponent(S.version || "")}`;
});

// ------------------------------------------------------------------
// 終了
// ------------------------------------------------------------------
document.getElementById("quit").addEventListener("click", async () => {
  if (desktop) {
    // 全ツールに「終わってよいか」を訊いてから、まとめて1つの確認(外枠の仕事)
    invoke("shell_close").catch(toastError);
    return;
  }
  try {
    await api.post("/api/shutdown", {});
    ended();
  } catch (err) {
    if (err.status === 409) {
      if (confirm(err.body.message || "実行中の処理があります。中断して終了しますか?")) {
        try { await api.post("/api/shutdown", { force: true }); ended(); } catch (e) { toastError(e); }
      }
      return;
    }
    toastError(err);
  }
});

function ended() {
  const dialog = document.getElementById("ended");
  for (const entry of frames.values()) entry.iframe?.remove();
  dialog.showModal();
}

document.getElementById("who").addEventListener("click", async () => {
  const text = document.getElementById("who").textContent;
  try {
    await navigator.clipboard.writeText(text);
    toast(`写しました: ${text}`);
  } catch (err) {
    window.prompt("この文を写してください", text);
  }
});

// ブラウザ版: 心拍(このタブを閉じたら、入口と各ツールのブラウザ版が自分で終わる)
function heartbeat() {
  if (desktop) return;
  const beat = () => api.post("/api/alive", {}).catch(() => {});
  beat();
  setInterval(beat, 20000);
}

settingsView.install({ desktop, invoke, tools: TOOLS, reloadTabs: followTabs });
heartbeat();
start();
// デスクトップ版は外枠に訊く(安い)。ブラウザ版は入口がツールの待ち受けを叩くので間を空ける
pollStatus();
setInterval(pollStatus, desktop ? (S.statusPollMs || 2000) : 5000);
