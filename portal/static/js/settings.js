/*
  settings.js — 大設定(タブ表示権限・共有の DB の置き場所・管理者パスワード・ツールの一覧)

  書き換えは管理者パスワードで鍵を開けてから(30分さわらなければ鍵が掛かる)。
  表は共有の DB に直接書き、書いたら手元の写しも読み直す(サーバの仕事)。
  2人が同時に同じ行を直したときは、あとの人に「読み直してから」を出す(409)。
*/
import { api } from "./api.js";
import { toast, toastError } from "./toast.js";

const MARK = { nippou: "日", kanban: "看", calendar: "暦", inspection: "点" };
const PHASE_LABEL = { idle: "まだ開いていません", starting: "起動中", started: "動作中",
                      failed: "止まっています", quit: "終了しました" };

let ctx = { desktop: false, invoke: null, tools: new Map(), reloadTabs: () => {} };
let view = null;
let editing = null;      // 直している行(新しい行なら null)
let phases = new Map();

const $ = (id) => document.getElementById(id);

export function install(options) {
  ctx = { ...ctx, ...options };
  $("lock-open").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api.post("/api/settings/auth", { password: $("lock-pass").value });
      $("lock-pass").value = "";
      toast("鍵を開けました(30分さわらなければ掛かります)");
      await refresh();
    } catch (err) { toastError(err); }
  });
  $("lock-close").addEventListener("click", async () => {
    try { await api.post("/api/settings/auth", { lock: true }); await refresh(); }
    catch (err) { toastError(err); }
  });
  $("rights-sync").addEventListener("click", async () => {
    try {
      const body = await api.post("/api/rights/sync", {});
      paint(body);
      const s = body.sync || {};
      toast(s.error ? s.error : (s.message || "読み直しました"), s.error ? "ng" : "ok");
      ctx.reloadTabs();
    } catch (err) { toastError(err); }
  });
  $("rights-create").addEventListener("click", () => write("/api/rights/create-table", {}));
  $("rights-add").addEventListener("click", () => openRow(null));
  $("row-cancel").addEventListener("click", () => $("row-dialog").close());
  $("row-form").addEventListener("submit", saveRow);
  for (const button of document.querySelectorAll("[data-fill]")) {
    button.addEventListener("click", () => {
      const me = view?.identity || {};
      if (button.dataset.fill === "login") $("row-login").value = me.login_id || "";
      else $("row-pc").value = me.pc_name || "";
    });
  }
  for (const button of document.querySelectorAll("[data-copy]")) {
    button.addEventListener("click", () => copy($(button.dataset.copy).textContent));
  }
  $("loc-form").addEventListener("submit", (event) => {
    event.preventDefault();
    write("/api/settings/location", { folder: $("loc-folder").value, name: $("loc-name").value });
  });
  $("loc-default").addEventListener("click", () => write("/api/settings/location", { folder: "", name: "" }));
  $("pw-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const body = await api.post("/api/settings/password", {
        current: $("pw-current").value, new: $("pw-new").value, confirm: $("pw-confirm").value });
      for (const id of ["pw-current", "pw-new", "pw-confirm"]) $(id).value = "";
      toast(body.message);
      await refresh();
    } catch (err) { toastError(err); }
  });
}

/** 大設定のタブを開いたとき */
export function opened() {
  refresh().catch(toastError);
}

export function decisionChanged(decision) {
  if (view && decision) {
    view.decision = decision;
    paintMe();
  }
}

export function statusChanged(list) {
  phases = new Map(list.map((item) => [item.id, item.phase]));
  if (view) paintTools();
}

async function refresh() {
  paint(await api.get("/api/settings"));
}

async function write(path, payload) {
  try {
    const body = await api.post(path, payload);
    paint(body);
    toast(body.message || "反映しました");
    ctx.reloadTabs();
    return true;
  } catch (err) {
    if (err.status === 403 && err.code === "locked") {
      toast("管理者パスワードで鍵を開けてください", "ng");
      $("lock-pass").focus();
    } else if (err.status === 409) {
      toastError(err);
      await refresh().catch(() => {});
    } else {
      toastError(err);
    }
    return false;
  }
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast(`写しました: ${text}`); }
  catch (err) { window.prompt("この文を写してください", text); }
}

// ------------------------------------------------------------------
// 描く
// ------------------------------------------------------------------
function paint(body) {
  view = body;
  const box = $("lockbox");
  box.dataset.open = body.admin ? "1" : "0";
  $("lock-state").textContent = body.admin ? "🔓 鍵が開いています" : "🔒 鍵が掛かっています";
  $("lock-open").hidden = Boolean(body.admin);
  $("lock-close").hidden = !body.admin;
  for (const node of document.querySelectorAll("[data-admin]")) {
    node.setAttribute("aria-disabled", body.admin ? "false" : "true");
    node.title = body.admin ? "" : "管理者パスワードで鍵を開けると使えます";
  }
  paintMe();
  paintRights();
  paintSource();
  paintTools();
  paintApp();
  $("pw-lead").textContent = body.password_custom
    ? "この端末で変えたパスワードが効いています。変えるには、いまのパスワードが要ります(4文字以上)。"
    : "まだ変えていません(ほかのツールと同じ既定のパスワード)。変えるには、いまのパスワードが要ります(4文字以上)。";
}

function chips(ids) {
  const wrap = document.createElement("span");
  wrap.className = "chips";
  for (const id of ids) {
    const tool = ctx.tools.get(id);
    if (!tool) continue;
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.dataset.mark = MARK[id] || tool.title.slice(0, 1);
    chip.style.setProperty("--c", `var(--tool-${id})`);
    chip.textContent = tool.title;
    wrap.append(chip);
  }
  return wrap;
}

function paintMe() {
  const me = view.identity || {};
  $("me-login").textContent = me.login_id || "(不明)";
  $("me-pc").textContent = me.pc_name || "(不明)";
  const d = view.decision || {};
  const tabs = $("me-tabs");
  tabs.replaceChildren(d.tabs && d.tabs.length ? chips(d.tabs) : document.createTextNode("(ツールのタブは出していません)"));
  $("me-reason").textContent = d.reason || "";
  const warn = $("me-warn");
  if (d.source === "unregistered") {
    warn.hidden = false;
    warn.textContent = `この端末は登録されていません。登録するには「+ 行を足す」で、PC名 ${me.pc_name || ""}`
      + `(またはログインID ${me.login_id || ""})の行を作ってください。`;
  } else if (d.source === "unreadable") {
    warn.hidden = false;
    warn.textContent = "共有フォルダにつながる状態で「共有から読み直す」を押すと、タブ表示権限で決め直します。";
  } else if (d.source === "unconfigured") {
    warn.hidden = false;
    warn.textContent = "まだ運用を始めていません。1行でも足すと、登録の無い端末にはツールのタブが出なくなります"
      + "(大設定だけになります)。先に、使う端末をまとめて登録してください。";
  } else {
    warn.hidden = true;
  }
}

function time(sec) {
  if (!sec) return "まだ";
  const d = new Date(sec * 1000);
  return d.toLocaleString("ja-JP", { hour12: false });
}

function paintSource() {
  const s = view.source || {};
  const line = $("rights-source");
  line.replaceChildren();
  const parts = [
    ["置き場所", s.path],
    ["決めたところ", s.origin],
    ["最後に読めた", time(s.imported_at)],
  ];
  for (const [label, value] of parts) {
    const span = document.createElement("span");
    span.innerHTML = "";
    const b = document.createElement("b");
    b.textContent = `${label}: `;
    span.append(b, document.createTextNode(value || "―"), document.createTextNode("　"));
    line.append(span);
  }
  if (s.error) {
    const err = document.createElement("div");
    err.className = "msg msg--warn";
    err.textContent = s.imported_at
      ? `${s.error}(前回読めた手元の写しで決めています)`
      : `${s.error}(手元の写しも無いので、すべてのタブを出しています)`;
    line.append(err);
  }
  $("rights-missing").hidden = s.state !== "missing";
  $("rights-table").textContent = s.table || "タブ表示権限";
  $("loc-folder").value = s.folder || "";
  $("loc-name").value = s.name || "";
  $("loc-note").textContent = `既定: ${s.default_folder}\\${s.default_name}`;
}

function paintRights() {
  const body = $("rights-grid").querySelector("tbody");
  body.replaceChildren();
  const me = view.identity || {};
  const rules = view.rules || [];
  const fold = (t) => String(t || "").normalize("NFKC").toLowerCase().trim();
  if (!rules.length) {
    const tr = document.createElement("tr");
    tr.className = "empty";
    const td = document.createElement("td");
    td.colSpan = 8;
    td.textContent = view.source?.state === "ok" ? "まだ1行もありません" : "表を読めていません";
    tr.append(td);
    body.append(tr);
  }
  for (const rule of rules) {
    const tr = document.createElement("tr");
    const mine = rule.enabled && (rule.login_id || rule.pc_name)
      && (!rule.login_id || fold(rule.login_id) === fold(me.login_id))
      && (!rule.pc_name || fold(rule.pc_name) === fold(me.pc_name));
    if (mine) tr.classList.add("is-mine");
    if (!rule.enabled) tr.classList.add("is-off");
    const cells = [rule.key ?? "", rule.login_id || "(問わない)", rule.pc_name || "(問わない)"];
    for (const value of cells) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    const tabs = document.createElement("td");
    tabs.append(chips(rule.tab_ids || []));
    if (rule.unknown && rule.unknown.length) {
      const bad = document.createElement("span");
      bad.className = "state";
      bad.dataset.phase = "failed";
      bad.textContent = ` 分からない: ${rule.unknown.join("、")}`;
      tabs.append(bad);
    }
    tr.append(tabs);
    for (const value of [rule.default_tab || "", rule.enabled ? "有効" : "無効", rule.note || ""]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    const act = document.createElement("td");
    act.className = "act";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "btn btn--small";
    edit.textContent = "直す";
    edit.dataset.admin = "";
    edit.addEventListener("click", () => openRow(rule));
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn--small btn--danger";
    del.textContent = "消す";
    del.addEventListener("click", () => removeRow(rule));
    act.append(edit, " ", del);
    tr.append(act);
    body.append(tr);
  }
  const problems = $("rights-problems");
  problems.replaceChildren();
  for (const text of view.problems || []) {
    const li = document.createElement("li");
    li.textContent = text;
    problems.append(li);
  }
  problems.hidden = !(view.problems || []).length;
}

function paintTools() {
  const body = $("tools-grid").querySelector("tbody");
  body.replaceChildren();
  for (const tool of view.tools || []) {
    const tr = document.createElement("tr");
    const tab = document.createElement("td");
    tab.append(chips([tool.id]));
    const name = document.createElement("td");
    name.textContent = tool.name;
    const ver = document.createElement("td");
    ver.textContent = tool.version ? `VER${tool.version}` : "";
    const state = document.createElement("td");
    const phase = phases.get(tool.id) || (ctx.desktop ? "idle" : "");
    const s = document.createElement("span");
    s.className = "state";
    s.dataset.phase = phase;
    s.textContent = phase ? PHASE_LABEL[phase] || phase : "(ブラウザ版はタブで確かめます)";
    state.append(s);
    const local = document.createElement("td");
    const code = document.createElement("code");
    code.textContent = tool.local;
    local.append(code);
    const act = document.createElement("td");
    act.className = "act";
    if (ctx.desktop && (phase === "failed" || phase === "quit")) {
      const again = document.createElement("button");
      again.type = "button";
      again.className = "btn btn--small";
      again.textContent = "もう一度開く";
      again.addEventListener("click", () => ctx.invoke("shell_restart_tool", { tool: tool.id })
        .then(() => toast(`${tool.title} を起こし直しています`)).catch(toastError));
      act.append(again);
    }
    tr.append(tab, name, ver, state, local, act);
    body.append(tr);
  }
}

function paintApp() {
  const a = view.app || {};
  const dl = $("app-facts");
  dl.replaceChildren();
  const rows = [
    ["名前", `${a.name} VER${a.version}`],
    ["動かし方", ctx.desktop ? "デスクトップ版(統合ツール.exe)" : "ブラウザ版(予備。Start.vbs)"],
    ["一式の場所", a.root],
    ["手元の領域", a.local],
    ["ログ", a.logs],
  ];
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    const code = document.createElement("code");
    code.textContent = value || "";
    dd.append(label === "名前" || label === "動かし方" ? document.createTextNode(value || "") : code);
    dl.append(dt, dd);
  }
}

// ------------------------------------------------------------------
// 行を足す・直す・消す
// ------------------------------------------------------------------
function openRow(rule) {
  if (!view?.admin) {
    toast("管理者パスワードで鍵を開けてください", "ng");
    $("lock-pass").focus();
    return;
  }
  editing = rule;
  $("row-title").textContent = rule ? `管理番号 ${rule.key} を直す` : "タブ表示権限に行を足す";
  $("row-login").value = rule ? rule.login_id : "";
  $("row-pc").value = rule ? rule.pc_name : (view.identity?.pc_name || "");
  $("row-enabled").checked = rule ? rule.enabled : true;
  $("row-note").value = rule ? rule.note : "";
  $("row-error").hidden = true;
  const checks = $("row-tabs");
  checks.replaceChildren();
  const chosen = new Set(rule ? rule.tab_ids || [] : []);
  for (const tool of view.catalog || []) {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = tool.id;
    box.checked = chosen.has(tool.id);
    label.append(box, document.createTextNode(tool.title));
    checks.append(label);
  }
  const select = $("row-default");
  select.replaceChildren(new Option("(指定しない)", ""));
  for (const tool of view.catalog || []) select.append(new Option(tool.title, tool.id));
  const current = (view.catalog || []).find((t) => rule && (t.title === rule.default_tab || t.id === rule.default_tab));
  select.value = current ? current.id : "";
  $("row-dialog").showModal();
}

async function saveRow(event) {
  event.preventDefault();
  const tabIds = [...$("row-tabs").querySelectorAll("input:checked")].map((b) => b.value);
  const payload = {
    login_id: $("row-login").value, pc_name: $("row-pc").value, tab_ids: tabIds,
    default_tab: $("row-default").value, enabled: $("row-enabled").checked, note: $("row-note").value,
  };
  if (!payload.login_id.trim() && !payload.pc_name.trim()) {
    $("row-error").hidden = false;
    $("row-error").textContent = "ログインID か PC名 の少なくとも一方を入れてください。";
    return;
  }
  // 最初の1行は、登録の無い端末からツールのタブを消す(運用の始まり)。確かめてから
  const effective = (view.rules || []).filter((r) => r.enabled && (r.login_id || r.pc_name));
  if (!editing && !effective.length && payload.enabled
      && !confirm("最初の1行です。\n\nこの行を足すと、タブ表示権限に登録の無い端末には、"
        + "次に開いたときからツールのタブが出なくなります(大設定だけになります)。\n\n続けますか?")) {
    return;
  }
  let ok;
  if (editing) {
    ok = await write("/api/rights/save", { ...payload, key: editing.key, was: wasOf(editing) });
  } else {
    ok = await write("/api/rights/add", payload);
  }
  if (ok) $("row-dialog").close();
}

function wasOf(rule) {
  return { login_id: rule.login_id, pc_name: rule.pc_name, tabs: rule.tabs,
           default_tab: rule.default_tab, enabled: rule.enabled, note: rule.note };
}

async function removeRow(rule) {
  if (!view?.admin) {
    toast("管理者パスワードで鍵を開けてください", "ng");
    return;
  }
  if (!confirm(`管理番号 ${rule.key}(${rule.condition})を消します。よろしいですか?`)) return;
  await write("/api/rights/delete", { key: rule.key, was: wasOf(rule) });
}
