/*
  inspection.js — 点検表の一覧・選択・表示テーマ(設定の画面は settings.js)
  (VBA版 UFPrintManager / ShowFolderSettings / ToggleDarkMode)

  選んだ順番を持つのはこの画面(印刷はその順で行う)。タブを開き直しても
  消えないよう sessionStorage に控える。
*/

import { api } from "./api.js";
import { toast, toastError } from "./toast.js";

const $ = (id) => document.getElementById(id);
const SEL_KEY = "isp.selection";
const VIEW_KEY = "isp.view";

export const state = {
  inventory: null,
  items: new Map(),     // id → 点検表
  order: [],            // 表示順の id
  selection: [],        // 選んだ順の id(この順で印刷する)
  activeCat: null,
  activeSub: null,
  search: "",
  settings: null,
  printing: false,
  printer: "",
  refreshing: false,    // 前回の一覧を出したまま、フォルダを確認している
};

const listeners = [];
/** 選択が変わったら呼ばれる(印刷・プレビューのボタン)。 */
export function onChange(fn) { listeners.push(fn); }

/* ---------------------------------------------------------------- 小物 */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
function load(key, fallback) {
  try { const v = sessionStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
}
function save(key, value) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* 控えられなくても動く */ }
}
const pad = (n) => String(n).padStart(2, "0");
export function formatDate(epochSec, withYear) {
  if (!epochSec) return "--";
  const d = new Date(epochSec * 1000);
  return `${withYear ? d.getFullYear() + "/" : ""}${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function normalize(text) {
  let s = String(text || "");
  try { s = s.normalize("NFKC"); } catch { /* 古いブラウザ */ }
  return s.toLowerCase();
}
/** VBA版の選択リストと同じ「[メイン - サブ] 名前」 */
export function itemLabel(item) {
  return `[${item.category} - ${item.subcategory}] ${item.name}`;
}
export function selectedItems() {
  return state.selection.map((id) => state.items.get(id)).filter(Boolean);
}

/* ---------------------------------------------------------------- 選択 */
const isSelected = (id) => state.selection.includes(id);

let hadSelection = false;

function commit() {
  // 選び始めたら Excel を裏で起動しておく(プレビュー・印刷を押したときに待たせない)
  const has = state.selection.length > 0;
  if (has && !hadSelection) api.post("/api/excel/warm").catch(() => { /* 先回りなので失敗しても続ける */ });
  hadSelection = has;
  save(SEL_KEY, state.selection);
  renderCategories();
  renderSubnav();
  syncItems();
  renderSelection();
  renderRibbon();
  for (const fn of listeners) fn();
}
function toggle(id) {
  const i = state.selection.indexOf(id);
  if (i >= 0) state.selection.splice(i, 1); else state.selection.push(id);
  commit();
}
function selectIds(ids) {
  let added = 0;
  for (const id of ids) if (!isSelected(id)) { state.selection.push(id); added += 1; }
  commit();
  return added;
}
function deselectIds(ids) {
  const drop = new Set(ids);
  const before = state.selection.length;
  state.selection = state.selection.filter((id) => !drop.has(id));
  commit();
  return before - state.selection.length;
}
function move(id, delta) {
  const i = state.selection.indexOf(id);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= state.selection.length) return;
  state.selection.splice(i, 1);
  state.selection.splice(j, 0, id);
  commit();
}

/* ---------------------------------------------------------------- 一覧 */
function currentCategory() {
  const cats = state.inventory ? state.inventory.categories : [];
  return cats.find((c) => c.name === state.activeCat) || cats[0] || null;
}
function currentSub(cat = currentCategory()) {
  if (!cat) return null;
  return cat.subcategories.find((s) => s.name === state.activeSub) || cat.subcategories[0] || null;
}
const idsOfCategory = (cat) => cat.subcategories.flatMap((s) => s.items.map((it) => it.id));
const countSelected = (ids) => ids.filter(isSelected).length;

function visibleItems() {
  if (!state.inventory) return [];
  if (state.search) {
    const q = normalize(state.search);
    return state.order.map((id) => state.items.get(id)).filter((it) =>
      normalize(`${it.name} ${it.file_name} ${it.category} ${it.subcategory}`).includes(q));
  }
  const sub = currentSub();
  return sub ? sub.items : [];
}

function applyInventory(inv, note) {
  state.inventory = inv;
  state.items = new Map();
  state.order = [];
  for (const cat of inv.categories) for (const sub of cat.subcategories) for (const it of sub.items) {
    state.items.set(it.id, it);
    state.order.push(it.id);
  }
  // **フォルダが見えないあいだは選択を消さない。** 共有の一時的な断で一覧が空に
  // なっただけなのに、選んでおいた点検表(とカテゴリの位置)まで解除して覚え直して
  // いた。見えたときに一覧と突き合わせる
  const reachable = inv.root_exists !== false;
  if (reachable) {
    const before = state.selection.length;
    state.selection = state.selection.filter((id) => state.items.has(id));
    if (before !== state.selection.length) {
      toast(`${before - state.selection.length} 件の選択を解除しました(一覧から無くなったため)`, "warn");
    }
    const cat = currentCategory();
    state.activeCat = cat ? cat.name : null;
    const sub = currentSub(cat);
    state.activeSub = sub ? sub.name : null;
    save(SEL_KEY, state.selection);
  }
  renderAll();
  if (note && reachable) toast(note, "ok");
  else if (note) toast("点検表フォルダが見えません。選んだものはそのまま残しています", "warn");
}

async function pollScan(overlay = true) {
  for (;;) {
    let data;
    try { data = await api.get("/api/inventory/status"); } catch { await sleep(1500); continue; }
    if (overlay) showScan(data.scan);
    if (data.scan.state !== "scanning") return;
    await sleep(overlay ? 400 : 700);
  }
}

/*
  **一覧が出せるなら、確認の終わりを待たせない。**
  起動直後は前回の一覧(控え)、再読込の途中はいまの一覧を出したまま、
  帯の「更新」に「確認中…」を出して裏で待つ。終わったら一覧を替える。
*/
let refreshing = null;

function refreshInBackground(note) {
  if (refreshing) return refreshing;
  state.refreshing = true;
  renderRibbon();
  refreshing = pollScan(false).then(() => {
    state.refreshing = false;
    refreshing = null;
    return loadInventory(note);
  });
  return refreshing;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 一覧を読む。検索中なら終わるまで覆いを出して待つ。 */
export async function loadInventory(note = "") {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    let data;
    try {
      data = await api.get("/api/inventory?wait=2");
    } catch (err) {
      hideScan();
      toastError(err);
      return;
    }
    state.settings = data.settings;
    if (data.scan && data.scan.state === "scanning") {
      if (data.inventory) {
        hideScan();
        applyInventory(data.inventory, "");
        refreshInBackground(note);
        return;
      }
      showScan(data.scan);
      await pollScan();
      continue;
    }
    hideScan();
    if (data.scan && data.scan.state === "error") toast(data.scan.message, "error");
    if (!data.inventory) {
      try { await api.post("/api/inventory/refresh"); } catch (err) { toastError(err); return; }
      continue;
    }
    applyInventory(data.inventory, note);
    return;
  }
}

export async function refresh() {
  try {
    await api.post("/api/inventory/refresh");
    await loadInventory("一覧を読み直しました");
  } catch (err) { toastError(err); }
}

function showScan(scan) {
  $("scan-overlay").hidden = false;
  $("scan-detail").textContent = `${scan.scanned || 0} 件確認済み${scan.current ? "\n" + scan.current : ""}`;
}
function hideScan() { $("scan-overlay").hidden = true; }

/* ---------------------------------------------------------------- 描画 */
function renderAll() {
  renderCategories();
  renderSubnav();
  renderItems();
  renderSelection();
  renderRibbon();
  for (const fn of listeners) fn();
}

function countBadge(selected, total, cls) {
  const b = el("span", cls, selected ? `${selected}/${total}` : String(total));
  if (selected) b.dataset.sel = "1";
  return b;
}

function renderCategories() {
  const box = $("cat-tabs");
  box.textContent = "";
  if (!state.inventory) return;
  for (const cat of state.inventory.categories) {
    const ids = idsOfCategory(cat);
    const tab = el("button", "tab");
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(cat.name === (currentCategory() || {}).name));
    if (state.search) tab.dataset.search = "1";
    tab.append(el("span", null, cat.name), countBadge(countSelected(ids), ids.length, "tab__badge"));
    tab.addEventListener("click", () => {
      state.activeCat = cat.name;
      state.activeSub = cat.subcategories[0] ? cat.subcategories[0].name : null;
      clearSearch();
      save(VIEW_KEY, { cat: state.activeCat, sub: state.activeSub });
      renderAll();
    });
    box.appendChild(tab);
  }
  const off = !currentCategory() || !!state.search;
  $("btn-cat-all").disabled = off;
  $("btn-cat-none").disabled = off;
}

function renderSubnav() {
  const box = $("subnav");
  box.textContent = "";
  const cat = currentCategory();
  if (!cat) return;
  const current = currentSub(cat);
  for (const sub of cat.subcategories) {
    const ids = sub.items.map((it) => it.id);
    const btn = el("button", "subnav__item");
    btn.type = "button";
    btn.title = sub.name;
    btn.setAttribute("aria-current", String(!state.search && current && sub.name === current.name));
    btn.append(el("span", "subnav__name", sub.name), countBadge(countSelected(ids), ids.length, "count"));
    btn.addEventListener("click", () => {
      state.activeSub = sub.name;
      clearSearch();
      save(VIEW_KEY, { cat: state.activeCat, sub: state.activeSub });
      renderCategories();
      renderSubnav();
      renderItems();
    });
    box.appendChild(btn);
  }
}

function showEmpty(title, message) {
  const box = $("items-empty");
  box.textContent = "";
  box.append(el("b", null, title), el("span", null, message));
  box.hidden = false;
}

function renderItems() {
  const grid = $("items");
  grid.textContent = "";
  $("items-empty").hidden = true;
  $("unmatched").hidden = true;
  const inv = state.inventory;
  const setTools = (on) => { $("btn-sub-all").disabled = !on; $("btn-sub-none").disabled = !on; };
  $("items-title").textContent = "点検表";
  $("items-count").textContent = "";
  if (!inv) { setTools(false); return; }
  if (!inv.root_exists) {
    showEmpty("点検表フォルダが見つかりません", `${inv.root}\n\n右上の「設定」→「点検表フォルダ」で正しいフォルダを指定してください。`);
    setTools(false);
    return;
  }
  if (!inv.categories.length) {
    showEmpty("点検表が見つかりません",
      "対象: .xlsm / .xlsx / .xls\nフォルダ構成: ルート > カテゴリー > サブカテゴリー > Excel");
    setTools(false);
    return;
  }
  const items = visibleItems();
  if (state.search) {
    $("items-title").textContent = "検索結果";
    $("items-count").textContent = `「${state.search}」 ${items.length} 件`;
    let group = "";
    for (const item of items) {
      const g = `${item.category} > ${item.subcategory}`;
      if (g !== group) { grid.appendChild(el("div", "group-title", g)); group = g; }
      grid.appendChild(itemCard(item));
    }
    if (!items.length) showEmpty("一致する点検表はありません", `「${state.search}」を含む点検表が見つかりませんでした。`);
  } else {
    const cat = currentCategory();
    const sub = currentSub(cat);
    $("items-title").textContent = `${cat.name} > ${sub ? sub.name : ""}`;
    $("items-count").textContent = `${items.length} 件`;
    for (const item of items) grid.appendChild(itemCard(item));
    if (sub && !items.length) showEmpty("表示できる点検表がありません", "このフォルダには命名規則に合う Excel ファイルがありません。");
    renderUnmatched(sub);
  }
  setTools(items.length > 0);
  syncItems();
}

function renderUnmatched(sub) {
  const box = $("unmatched");
  if (!sub || !sub.unmatched || !sub.unmatched.length) { box.hidden = true; return; }
  $("unmatched-summary").textContent = `命名規則に合わないため表示していないファイル: ${sub.unmatched.length} 件`;
  const list = $("unmatched-list");
  list.textContent = "";
  for (const u of sub.unmatched) list.appendChild(el("li", null, u.file_name));
  box.hidden = false;
}

function itemCard(item) {
  const card = el("label", "item");
  card.dataset.id = item.id;
  card.title = `${item.file_name}\n更新: ${formatDate(item.modified, true)}`;
  const input = document.createElement("input");
  input.type = "checkbox";
  input.addEventListener("change", () => toggle(item.id));
  const body = el("span", "item__body");
  body.append(el("span", "item__name", item.name),
              el("span", "item__meta", `${item.ext.toUpperCase()} ・ 更新 ${formatDate(item.modified, true)}`));
  card.append(input, body);
  return card;
}

/** チェックと選んだ順の札だけを直す(描き直さない) */
function syncItems() {
  for (const card of $("items").querySelectorAll(".item")) {
    const pos = state.selection.indexOf(card.dataset.id);
    card.querySelector("input").checked = pos >= 0;
    card.dataset.selected = pos >= 0 ? "1" : "0";
    let badge = card.querySelector(".item__order");
    if (pos >= 0) {
      if (!badge) { badge = el("span", "item__order"); card.appendChild(badge); }
      badge.textContent = String(pos + 1);
    } else if (badge) badge.remove();
  }
}

let previewOpener = () => {};
/** 選択一覧の目のボタンから開くプレビュー(preview.js が登録する)。 */
export function setPreviewOpener(fn) { previewOpener = fn; }

function iconButton(glyph, label, onClick, disabled, extra = "") {
  const b = el("button", `ib ${extra}`.trim(), glyph);
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.disabled = !!disabled;
  b.addEventListener("click", onClick);
  return b;
}

function renderSelection() {
  const list = $("sel-list");
  list.textContent = "";
  const n = state.selection.length;
  $("sel-count").textContent = String(n);
  $("sel-empty").hidden = n > 0;
  $("btn-clear-sel").disabled = n === 0 || state.printing;
  state.selection.forEach((id, index) => {
    const item = state.items.get(id);
    if (!item) return;
    const li = el("li", "sel-item");
    const text = el("div", "sel-item__text");
    text.title = itemLabel(item);
    text.append(el("span", "sel-item__name", item.name),
                el("span", "sel-item__path", `${item.category} - ${item.subcategory}`));
    const acts = el("div", "sel-item__acts");
    acts.append(
      iconButton("◧", "プレビュー", () => previewOpener(id)),
      iconButton("↑", "上へ(印刷順)", () => move(id, -1), index === 0 || state.printing),
      iconButton("↓", "下へ(印刷順)", () => move(id, 1), index === n - 1 || state.printing),
      iconButton("×", "選択を解除", () => deselectIds([id]), state.printing, "ib--x"));
    li.append(el("span", "sel-item__no", `${index + 1}.`), text, acts);
    list.appendChild(li);
  });
}

function renderRibbon() {
  const inv = state.inventory;
  $("rb-selected").textContent = `${state.selection.length}件`;
  const folder = $("rb-folder");
  const root = inv ? inv.root : (state.settings ? state.settings.root_folder : "");
  folder.firstElementChild.textContent = root || "(未設定)";
  folder.title = root || "";
  if (inv && !inv.root_exists) folder.dataset.state = "unset"; else delete folder.dataset.state;
  if (inv) {
    const found = $("rb-found");
    found.textContent = inv.unmatched_files ? `${inv.total_files}件(表示${inv.listed_files})` : `${inv.total_files}件`;
    found.title = inv.errors && inv.errors.length ? `読み取れないフォルダが ${inv.errors.length} 件あります` : "";
    if (inv.errors && inv.errors.length) found.dataset.state = "unset"; else delete found.dataset.state;
    $("rb-scanned").textContent = state.refreshing ? "確認中…" : formatDate(inv.scanned_at).slice(-5);
    $("rb-scanned").title = state.refreshing
      ? `前回(${formatDate(inv.scanned_at, true)})の一覧を表示しています。点検表フォルダを確認しています`
      : formatDate(inv.scanned_at, true);
  }
}

function clearSearch() {
  if (!state.search) return;
  state.search = "";
  $("search").value = "";
}

/** 印刷中は選び直しや読み直しをさせない(印刷は選んだ順に進んでいる)。 */
export function setPrinting(active) {
  state.printing = !!active;
  $("btn-refresh").disabled = state.printing;
  $("btn-settings").disabled = state.printing;
  renderSelection();
  for (const fn of listeners) fn();
}

/* ---------------------------------------------------------------- 部数 */
/** VBA版 DoPrint と同じ: 数値なら四捨五入して 1～上限。数値でなければ null。 */
export function getCopies() {
  const raw = String($("copies").value).trim().replace(/[０-９]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xFEE0));
  if (raw === "" || Number.isNaN(Number(raw))) return null;
  return Math.min(window.APP.maxCopies || 100, Math.max(1, Math.round(Number(raw))));
}
function setCopies(n) {
  $("copies").value = String(Math.min(window.APP.maxCopies || 100, Math.max(1, n)));
  $("copies").removeAttribute("aria-invalid");
}

/* ---------------------------------------------------------------- テーマ */
function paintThemeButton() {
  // VBA版と同じく、ボタンには「切り替え先」を出す
  const dark = document.documentElement.dataset.theme === "dark";
  $("btn-theme").textContent = dark ? "ライト" : "ダーク";
}
async function toggleTheme() {
  const dark = document.documentElement.dataset.theme !== "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  paintThemeButton();
  try { await api.post("/api/settings", { dark_mode: dark }); } catch (err) { toastError(err); }
}

/* ---------------------------------------------------------------- 初期化 */
export function init() {
  state.selection = load(SEL_KEY, []);
  const view = load(VIEW_KEY, {});
  state.activeCat = view.cat || null;
  state.activeSub = view.sub || null;
  paintThemeButton();

  $("btn-refresh").addEventListener("click", refresh);
  $("btn-theme").addEventListener("click", toggleTheme);

  $("btn-cat-all").addEventListener("click", () => {
    const cat = currentCategory();
    if (cat && !selectIds(idsOfCategory(cat))) toast("このカテゴリはすべて選択済みです");
  });
  $("btn-cat-none").addEventListener("click", () => {
    const cat = currentCategory();
    if (cat) deselectIds(idsOfCategory(cat));
  });
  $("btn-sub-all").addEventListener("click", () => selectIds(visibleItems().map((it) => it.id)));
  $("btn-sub-none").addEventListener("click", () => deselectIds(visibleItems().map((it) => it.id)));
  $("btn-clear-sel").addEventListener("click", () => { state.selection = []; commit(); });

  let timer = null;
  $("search").addEventListener("input", (ev) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.search = ev.target.value.trim();
      renderCategories();
      renderSubnav();
      renderItems();
    }, 120);
  });
  $("search").addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { clearSearch(); renderAll(); }
  });

  $("copies-dec").addEventListener("click", () => setCopies((getCopies() || 1) - 1));
  $("copies-inc").addEventListener("click", () => setCopies((getCopies() || 0) + 1));
  $("copies").addEventListener("input", () => {
    if (getCopies() === null) $("copies").setAttribute("aria-invalid", "true");
    else $("copies").removeAttribute("aria-invalid");
  });
  $("copies").addEventListener("blur", () => { const n = getCopies(); if (n !== null) setCopies(n); });


  renderSelection();
  renderRibbon();
  api.get("/api/printer").then((data) => {
    state.printer = data.printer || "";
    $("printer-name").textContent = `プリンター: ${state.printer || "既定のプリンター"}`;
  }).catch(() => { /* 表示だけなので無視 */ });
  return loadInventory();
}

/** 表に戻った・再接続したとき、別の場所で一覧が変わっていれば読み直す。 */
export async function resync() {
  try {
    const data = await api.get("/api/inventory/status");
    const scan = data.scan || {};
    if (scan.state === "scanning" && $("scan-overlay").hidden) { await loadInventory(); return; }
    if (state.inventory && scan.scanned_at && scan.scanned_at !== state.inventory.scanned_at) {
      await loadInventory("一覧が更新されていたので読み直しました");
    }
  } catch { /* 繋がっていなければ赤い帯が出ている */ }
}
