/*
  views/logs.js — 設定・管理者の「ログ」の面

    エラー等の後追いが現状できないと感じている
    ログを残しなぜなぜで分析できるようにしておいてほしい

  **並べるだけ。** 何を一覧に出すか・経過をどこまで拾うか・シートの形は
  サーバ(`nippou/logic/event_log.py`)が決めます。ここは受け取って描きます。

  面を開いたときに初めて読みます ── 設定画面を開くたびに記録を読むと、
  ほかの面を使う人まで待たせるので。
*/

import { api } from "../api.js";
import { pageSignal } from "../nav.js";
import { current } from "../tabs.js";
import { toast, toastError } from "../toast.js";

const $ = (id) => document.getElementById(id);

let loaded = false;
let openId = "";

function isoDay(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function el(tag, attrs = {}, text = "") {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  if (text) node.textContent = text;
  return node;
}

function note(text, kind = "info") {
  const box = $("log-msg");
  if (!box) return;
  box.hidden = !text;
  box.className = `msg msg--${kind}`;
  box.textContent = text || "";
}

function filters() {
  return {
    start: $("log-start").value,
    end: $("log-end").value,
    scope: $("log-scope").value || "errors",
    terminal: $("log-terminal").value,
    q: $("log-q").value.trim(),
  };
}

function fillSelect(select, items, keep) {
  const before = keep ?? select.value;
  select.replaceChildren(...items.map(([value, label]) => {
    const opt = el("option", { value }, label);
    if (value === before) opt.selected = true;
    return opt;
  }));
}

function kindBadge(row) {
  const cls = { error: "badge--alert", client: "badge--alert", warn: "badge--todo",
                refused: "badge--todo" }[row.kind] || "";
  return el("span", { class: `badge ${cls}`.trim() }, row.kind_label);
}

function statusBadge(row) {
  if (!row.status_label) {
    // エラーなのにまだ書いていないものだけ「未」と出す(操作や断りは要らない)
    return (row.kind === "error" || row.kind === "client")
      ? el("span", { class: "badge badge--todo" }, "未") : el("span", {}, "");
  }
  const cls = row.status === "done" ? "badge--done" : "badge--todo";
  return el("span", { class: `badge ${cls}` }, row.status_label);
}

function paintList(body) {
  const status = body.status || {};
  $("log-dir").textContent = status.dir || "";
  const fallback = $("log-fallback");
  const problem = status.fallback || status.text_problem || "";
  fallback.hidden = !problem;
  fallback.textContent = problem
    ? `${problem} ── いまはこの端末の ${status.default_dir} へ書いています。共有に届けば元の先へ戻ります。`
    : "";

  fillSelect($("log-scope"), (body.scopes || []).map((s) => [s.key, s.label]),
             body.filters?.scope);
  fillSelect($("log-terminal"),
             [["", "全部"], ...(body.terminals || []).map((t) => [t, t])],
             body.filters?.terminal);
  if (!$("log-start").value) $("log-start").value = body.start;
  if (!$("log-end").value) $("log-end").value = body.end;

  const c = body.counts || {};
  $("log-counts").textContent =
    `${body.start} 〜 ${body.end}: エラー ${c.errors ?? 0}件 / `
    + `エラー・警告・断り ${c.problems ?? 0}件 / 操作も全部 ${c.all ?? 0}件`
    + ` ── ${(body.rows || []).length}件を表示`;

  const rows = body.rows || [];
  const tbody = $("log-rows");
  if (!rows.length) {
    tbody.replaceChildren(el("tr", {}, ""));
    tbody.firstChild.append(el("td", { colspan: "8", class: "lead" },
      "この期間・この絞り込みでは記録がありません"));
    return;
  }
  tbody.replaceChildren(...rows.map((row) => {
    const tr = el("tr", { class: "log-row", tabindex: "0", dataset: { id: row.id },
                          "aria-label": `${row.id} を開く` });
    if (row.id === openId) tr.classList.add("is-open");
    tr.append(
      el("td", { class: "num" }, row.at),
      el("td", {}, ""),
      el("td", {}, ""),
      el("td", {}, row.terminal),
      el("td", {}, row.screen),
      el("td", {}, row.label),
      el("td", { class: "log-message", title: row.message }, row.message),
      el("td", {}, ""),
    );
    tr.children[1].append(el("code", {}, row.id));
    tr.children[2].append(kindBadge(row));
    if (row.count) tr.children[2].append(el("span", { class: "lead", title: row.count }, " ＋"));
    tr.children[7].append(statusBadge(row));
    return tr;
  }));
}

async function loadList() {
  const f = filters();
  const query = new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString();
  try {
    paintList(await api.get(`/api/log/events?${query}`));
    note("");
    loaded = true;
  } catch (err) {
    note(err.message, "error");
  }
}

function kv(box, items) {
  box.replaceChildren(...items.flatMap((item) => [
    el("dt", {}, item.label), el("dd", {}, item.value),
  ]));
}

function paintSheet(sheet, body = {}) {
  openId = sheet.id;
  $("log-sheet").hidden = false;
  $("ls-id").textContent = sheet.id;
  kv($("ls-facts"), sheet.facts || []);
  kv($("ls-cause"), sheet.cause?.length ? sheet.cause
                                         : [{ label: "記録", value: "例外はありません(断り・警告)" }]);
  kv($("ls-state"), sheet.state || []);
  $("ls-steps").replaceChildren(...(sheet.steps || []).map((step) => {
    const li = el("li", { class: `why-step why-step--${step.kind}` }, step.text);
    if (step.is_target) li.classList.add("is-target");
    return li;
  }));
  const input = Object.entries(sheet.input || {});
  $("ls-input-box").hidden = !input.length;
  kv($("ls-input"), input.map(([label, value]) => ({ label, value })));
  const crumbs = sheet.crumbs || [];
  $("ls-crumbs-box").hidden = !crumbs.length;
  $("ls-crumbs").replaceChildren(...crumbs.map((c) => el("li", {}, c)));
  $("ls-trace-box").hidden = !sheet.trace;
  $("ls-trace").textContent = sheet.trace || "";
  $("ls-hint").textContent = sheet.hint ? `手がかり(記録から): ${sheet.hint}` : "";

  const a = sheet.analysis || {};
  const count = body.why_count || (a.whys || []).length || 5;
  $("ls-whys").replaceChildren(...Array.from({ length: count }, (_, i) => {
    const li = el("li", { class: "why-field" });
    const id = `ls-why-${i}`;
    li.append(el("label", { for: id },
                 i === 0 ? "なぜ1 ── なぜそれが起きたか" : `なぜ${i + 1} ── なぜ${i}の答えは、なぜそうなったか`));
    const area = el("textarea", { id, rows: "2", maxlength: "400", "data-why": String(i) });
    area.value = (a.whys || [])[i] || "";
    li.append(area);
    return li;
  }));
  $("ls-measure").value = a.measure || "";
  if (body.statuses) {
    fillSelect($("ls-status"), body.statuses.map((s) => [s.key, s.label]), a.status || "open");
  }
  $("ls-status").value = a.status || "open";
  $("ls-by").value = a.by || "";
  $("ls-saved").textContent = a.at
    ? `前に残したもの: ${a.at.replace("T", " ")}(${a.terminal || ""})` : "まだ書いていません";
  for (const tr of document.querySelectorAll(".log-row")) {
    tr.classList.toggle("is-open", tr.dataset.id === openId);
  }
}

async function openSheet(id) {
  try {
    const body = await api.get(`/api/log/event/${encodeURIComponent(id)}`);
    paintSheet(body.sheet, body);
    $("log-sheet").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    toastError(err);
  }
}

async function saveSheet() {
  if (!openId) return;
  const whys = [...document.querySelectorAll("#ls-whys [data-why]")].map((t) => t.value);
  try {
    const body = await api.post(`/api/log/event/${encodeURIComponent(openId)}/analysis`, {
      whys, measure: $("ls-measure").value, status: $("ls-status").value,
      by: $("ls-by").value,
    });
    paintSheet(body.sheet);
    toast(body.message, "ok");
    loadList();
  } catch (err) {
    toastError(err);
  }
}

async function exportCsv() {
  try {
    const body = await api.post("/api/log/csv", filters());
    note(body.message, "ok");
  } catch (err) {
    note(err.message, "error");
  }
}

export function start() {
  const root = $("settingsTabs");
  if (!root || !$("panel-logs")) return;
  const signal = pageSignal();
  loaded = false;
  openId = "";

  const today = new Date();
  const from = new Date(today);
  from.setDate(today.getDate() - 6);
  $("log-start").value = isoDay(from);
  $("log-end").value = isoDay(today);

  const shown = (key) => { if (key === "logs" && !loaded) loadList(); };
  root.addEventListener("tab:select", (event) => shown(event.detail.key), { signal });
  shown(current(root));

  $("log-reload").addEventListener("click", loadList, { signal });
  for (const id of ["log-scope", "log-terminal", "log-start", "log-end"]) {
    $(id).addEventListener("change", loadList, { signal });
  }
  $("log-q").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadList();
  }, { signal });
  $("log-csv").addEventListener("click", exportCsv, { signal });
  $("log-rows").addEventListener("click", (event) => {
    const tr = event.target.closest(".log-row");
    if (tr) openSheet(tr.dataset.id);
  }, { signal });
  $("log-rows").addEventListener("keydown", (event) => {
    const tr = event.target.closest(".log-row");
    if (tr && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openSheet(tr.dataset.id);
    }
  }, { signal });
  $("ls-close").addEventListener("click", () => {
    $("log-sheet").hidden = true;
    openId = "";
    for (const tr of document.querySelectorAll(".log-row")) tr.classList.remove("is-open");
  }, { signal });
  $("ls-save").addEventListener("click", saveSheet, { signal });

  // 番号を添えて開かれたとき(`/settings?tab=logs&id=E1001-…`)は、その1件を出す
  const wanted = new URLSearchParams(location.search).get("id");
  if (wanted) {
    $("log-q").value = wanted;
    openSheet(wanted);
  }
}
