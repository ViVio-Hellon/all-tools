/*
  preview.js — 点検表のプレビュー(VBA版 UFPreview)

  Excel で点検表を開き、CopyPicture で作った画像を出す。
  画像はサーバが控えるので、2回目からはすぐに出る。
*/

import { api, tokenUrl } from "./api.js";
import * as sheet from "./inspection.js";
import * as running from "./running.js";
import { toast } from "./toast.js";

const $ = (id) => document.getElementById(id);
const view = { items: [], index: -1, req: 0, fit: true };
// ↑↓で送っているあいだは、止まってから頼む。押しっぱなしで全部の点検表を
// Excel に作らせない(サーバも、次の要求が来た前の要求は順番待ちをやめる)
const KEY_SETTLE_MS = 180;
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

export function open(focusId) {
  view.items = sheet.selectedItems();
  if (!view.items.length) {
    toast("プレビューする点検表を選んでください。", "warn");
    return;
  }
  const list = $("pv-list");
  list.textContent = "";
  view.items.forEach((item, index) => {
    const li = el("li", "preview__item");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", "false");
    li.title = sheet.itemLabel(item);
    const label = el("span");
    label.append(el("b", null, item.name), el("small", null, `${item.category} - ${item.subcategory}`));
    li.append(el("span", "preview__no", String(index + 1)), label);
    li.addEventListener("click", () => select(index));
    list.appendChild(li);
  });
  const dlg = $("preview-dialog");
  if (!dlg.open) dlg.showModal();
  const start = Math.max(0, view.items.findIndex((it) => it.id === focusId));
  select(start);
}

function select(index, force = false, settleMs = 0) {
  if (index < 0 || index >= view.items.length) return;
  view.index = index;
  [...$("pv-list").children].forEach((node, i) => {
    node.setAttribute("aria-selected", String(i === index));
    if (i === index) node.scrollIntoView({ block: "nearest" });
  });
  show(view.items[index], force, settleMs);
}

function message(nodes, isError = false) {
  const box = $("pv-message");
  box.textContent = "";
  if (isError) box.dataset.error = "1"; else delete box.dataset.error;
  for (const n of nodes || []) box.appendChild(n);
}

async function show(item, force, settleMs = 0) {
  const no = ++view.req;
  const img = $("pv-image");
  img.hidden = true;
  img.removeAttribute("src");
  $("pv-status").textContent = "プレビューを作成しています...";
  message([el("span", "spinner"), el("b", null, "Excel で画像を作成しています"),
           el("span", null, "初回は数秒～十数秒かかります(2回目からはすぐに表示されます)")]);
  if (settleMs) {
    await sleep(settleMs);
    if (no !== view.req) return;           // まだ送っている途中
  }
  running.set({ label: "プレビュー", message: item.name, current: 0, total: 0 });
  try {
    const data = await api.post("/api/preview", { id: item.id, force });
    if (no !== view.req) return;           // 別の点検表に切り替え済み
    img.onload = () => { if (no === view.req) { message([]); img.hidden = false; } };
    img.onerror = () => { if (no === view.req) fail(item, "プレビュー画像を読み込めませんでした。", ""); };
    img.alt = `${item.name} のプレビュー`;
    img.src = tokenUrl(data.image_url);
    const info = [];
    if (data.sheet) info.push(`シート: ${data.sheet}`);
    if (data.range) info.push(`範囲: ${data.range}`);
    if (data.cached) info.push("控えの画像");
    $("pv-status").textContent = `表示中: ${item.name}${info.length ? `(${info.join(" / ")})` : ""}`;
  } catch (err) {
    if (no === view.req) fail(item, err.message, err.detail, err.ref);
  } finally {
    if (no === view.req) running.set(null);
  }
}

function fail(item, text, detail, ref = "") {
  $("pv-status").textContent = `表示できません: ${item.name}`;
  const retry = el("button", "btn btn--sm", "もう一度");
  retry.type = "button";
  retry.addEventListener("click", () => select(view.index, true));
  const nodes = [el("b", null, text)];
  if (detail) nodes.push(el("span", "detail", detail));
  if (ref) nodes.push(el("span", "detail", `問い合わせ番号: ${ref}(設定 →「ログ」で原因を辿れます)`));
  nodes.push(retry);
  message(nodes, true);
}

function setFit(fit) {
  view.fit = fit;
  $("pv-view").classList.toggle("fit", fit);
  $("pv-fit").textContent = fit ? "全体表示" : "実寸表示";
  $("pv-fit").setAttribute("aria-pressed", String(fit));
}

export function init() {
  sheet.setPreviewOpener(open);
  $("btn-preview").addEventListener("click", () => open());
  $("pv-close").addEventListener("click", () => $("preview-dialog").close());
  $("pv-regenerate").addEventListener("click", () => select(view.index, true));
  $("pv-fit").addEventListener("click", () => setFit(!view.fit));
  $("pv-image").addEventListener("click", () => setFit(!view.fit));
  $("preview-dialog").addEventListener("close", () => { view.req += 1; running.set(null); });
  $("preview-dialog").addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") { ev.preventDefault(); select(Math.min(view.index + 1, view.items.length - 1), false, KEY_SETTLE_MS); }
    if (ev.key === "ArrowUp") { ev.preventDefault(); select(Math.max(view.index - 1, 0), false, KEY_SETTLE_MS); }
  });
  setFit(true);
}
