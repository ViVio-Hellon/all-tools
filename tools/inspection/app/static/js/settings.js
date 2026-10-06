/*
  settings.js — 設定(タブで3つの面に分ける)

    点検表フォルダ   … VBA版 ShowFolderSettings / SelectFolderDialog
    管理者パスワード … 配布設定を書き出す・読み込み直す・消すときの確認
    配布設定         … 1台で決めた設定を、ほかのラインの端末でそのまま使う
                       (python-web-tools の「配布設定」を移植)

  タブの動きは python-web-tools の tabs.js をそのまま使う(矢印キーで移動・
  前回見ていた面を覚える)。**判断はすべてサーバ**で、ここは送って、返って
  きた状態を描くだけ。パスワードの値は画面に残さない。
*/

import { api } from "./api.js";
import * as desktop from "./desktop.js";
import * as sheet from "./inspection.js";
import * as logs from "./logs.js";
import * as tabs from "./tabs.js";
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

/* ---------------------------------------------------------------- 描く */
function render(data) {
  const s = data.settings;
  sheet.state.settings = s;

  // 点検表フォルダ
  const source = { user: "この PC で設定したフォルダ", config: "既定(config\\inspection.json)",
                   demo: "模擬モードの見本フォルダ" };
  $("settings-source").textContent = `いまの設定: ${source[s.root_folder_source] || ""}` +
    (s.root_folder_source === "user" ? ` / 既定: ${s.root_folder_default}` : "");
  const locked = s.root_folder_source === "demo";
  $("settings-save").disabled = locked;
  $("settings-folder").readOnly = locked;
  $("settings-pick").disabled = locked;
  $("settings-reset").disabled = s.root_folder_source !== "user";

  // 管理者パスワード
  const custom = data.admin && data.admin.custom;
  $("adm-state").className = `st st--${custom ? "ok" : "warn"}`;
  $("adm-state").textContent = custom ? "変更済み" : "既定のまま";
  $("adm-min").textContent = String((data.admin && data.admin.min_length) || 4);

  renderDistribution(data.distribution);

  // 見出しにも状態を出す(開いていない面でも分かる)
  const dist = data.distribution || {};
  tabs.setBadges($("settings-tabs"), {
    password: custom ? {} : { text: "既定", level: "warn" },
    distribution: dist.exists ? { text: "あり", level: "ok" } : {},
  });
}

function renderDistribution(dist) {
  if (!dist) return;
  $("dist-state").textContent = dist.exists ? "あり" : "なし";
  $("dist-state").className = `st st--${dist.exists ? "ok" : "warn"}`;
  $("dist-meta").textContent = dist.exists
    ? `${dist.created_at} に ${dist.created_on} で作成${dist.created_version ? `(VER${dist.created_version})` : ""}`
    : "まだありません。下で書き出すと、アプリのフォルダの直下に「配布設定」フォルダができます。";
  $("dist-rows").replaceChildren(...dist.contents.map((c) => {
    const tr = el("tr");
    tr.append(el("td", "t", c.label), el("td", "t", c.value));
    return tr;
  }));
  $("dist-table").hidden = !dist.exists;
  $("dist-path").textContent = dist.path;
  $("dist-applied").textContent = dist.applied_at
    ? `この端末が最後に読み込んだ(または書き出した)とき: ${dist.applied_at}`
    : "この端末はまだ配布設定を読み込んでいません。";

  // 入れる項目。**この端末のいまの値**を添える(何が配られるかを押す前に見せる)
  // **利用者が自分で付け外ししたものだけ**描き直しても残す。それ以外は既定に従う
  // (最初に開いたときは未設定で選べなかった項目も、設定したあとは既定で入る)
  const box = $("dist-items");
  const was = new Map([...box.querySelectorAll("input[data-dist-item][data-touched]")]
    .map((i) => [i.dataset.distItem, i.checked]));
  box.replaceChildren(el("legend", null, "入れる設定(この端末のいまの値)"), ...dist.items.map((it) => {
    const label = el("label", "distchecks__item");
    const input = el("input");
    input.type = "checkbox";
    input.dataset.distItem = it.key;
    input.checked = was.has(it.key) ? was.get(it.key) : (it.default && it.set);
    input.disabled = !it.set;
    input.addEventListener("change", () => { input.dataset.touched = "1"; });
    const text = el("span", "distchecks__text");
    text.append(el("b", null, it.label), el("small", null, it.current));
    label.append(input, text);
    label.title = it.set ? "" : "この端末で変えていないので、既定のまま(入れなくても配った先は既定で動きます)";
    return label;
  }));
}

/* ---------------------------------------------------------------- 開く */
export async function open() {
  why("settings-error", "");
  let data;
  try { data = await api.get("/api/settings"); } catch (err) { toastError(err); return; }
  render(data);
  $("settings-folder").value = data.settings.root_folder || "";
  $("settings-dialog").showModal();
  if (tabs.current($("settings-tabs")) === "folder") $("settings-folder").focus();
  if (tabs.current($("settings-tabs")) === "logs") logs.load();
  browseTo(data.settings.root_folder);
}

/* ---------------------------------------------------------------- 点検表フォルダ */
async function browseTo(path) {
  let data;
  try { data = await api.get(`/api/fs/list?path=${encodeURIComponent(path || "")}`); } catch (err) {
    $("fs-note").textContent = err.message;
    return;
  }
  $("fs-path").firstElementChild.textContent = data.path || "";
  $("fs-path").title = data.path || "";
  $("fs-up").disabled = !data.parent;
  $("fs-up").dataset.parent = data.parent || "";
  const roots = $("fs-roots");
  roots.textContent = "";
  roots.appendChild(el("option", null, "ドライブ…")).value = "";
  for (const r of data.roots) roots.appendChild(el("option", null, r.name)).value = r.path;
  const list = $("fs-list");
  list.textContent = "";
  for (const d of data.dirs) {
    const li = el("li");
    const b = el("button", null, `▸ ${d.name}`);
    b.type = "button";
    b.title = "開く";
    b.addEventListener("click", () => { $("settings-folder").value = d.path; browseTo(d.path); });
    li.appendChild(b);
    list.appendChild(li);
  }
  for (const f of data.excel_files) list.appendChild(el("li", "fs__file", f));
  if (data.excel_count > data.excel_files.length) {
    list.appendChild(el("li", "fs__file", `ほか Excel ${data.excel_count - data.excel_files.length} 件`));
  }
  $("fs-note").textContent = data.message || `フォルダ ${data.dirs.length} 件 ・ Excel ${data.excel_count} 件`;
}

async function saveFolder(body) {
  why("settings-error", "");
  try {
    const data = await api.post("/api/settings", body);
    $("settings-dialog").close();
    sheet.state.settings = data.settings;
    sheet.state.activeCat = null;
    sheet.state.activeSub = null;
    await sheet.loadInventory("点検表フォルダを変更しました。一覧を読み直しました。");
  } catch (err) {
    why("settings-error", err.message + (err.detail ? `\n${err.detail}` : ""));
  }
}

/* ---------------------------------------------------------------- 管理者パスワード */
async function sendPassword(body) {
  why("adm-why", "");
  try {
    const data = await api.post("/api/settings/admin-password", body);
    render(data);
    toast(data.message || "変えました", "ok");
  } catch (err) {
    why("adm-why", err.message);                  // 断りの理由は押した場所のそばに出す
  } finally {
    // 打った値は残さない。肩越しに見られる時間を短くする
    for (const id of ["adm-now", "adm-new", "adm-confirm"]) $(id).value = "";
  }
}

/* ---------------------------------------------------------------- 配布設定 */
async function sendDistribution(path, body) {
  why("dist-why", "");
  try {
    const data = await api.post(path, { ...body, password: $("dist-password").value });
    render(data);
    toast(data.message || "済みました", "ok");
    if (data.rescan) {
      // 点検表フォルダが替わった。一覧を読み直す
      sheet.state.activeCat = null;
      sheet.state.activeSub = null;
      sheet.loadInventory();
    }
  } catch (err) {
    why("dist-why", err.message);
  } finally {
    $("dist-password").value = "";
  }
}

/* ---------------------------------------------------------------- 初期化 */
export function init() {
  tabs.attach($("settings-tabs"));
  $("btn-settings").addEventListener("click", open);
  $("settings-close").addEventListener("click", () => $("settings-dialog").close());

  $("settings-save").addEventListener("click", () => {
    const folder = $("settings-folder").value.trim();
    if (!folder) { why("settings-error", "点検表フォルダを入力してください。"); return; }
    saveFolder({ root_folder: folder });
  });
  $("settings-reset").addEventListener("click", () => saveFolder({ reset_root_folder: true }));
  $("settings-folder").addEventListener("keydown", (ev) => { if (ev.key === "Enter") $("settings-save").click(); });
  $("fs-up").addEventListener("click", () => {
    const parent = $("fs-up").dataset.parent;
    if (parent) { $("settings-folder").value = parent; browseTo(parent); }
  });
  $("fs-roots").addEventListener("change", (ev) => { if (ev.target.value) browseTo(ev.target.value); });
  // デスクトップ版: Windows の「フォルダーの選択」窓(共有フォルダもそのまま選べる)
  $("settings-pick").addEventListener("click", async () => {
    try {
      const picked = await desktop.pickFolder($("settings-folder").value.trim(), "点検表フォルダを選ぶ");
      if (picked) { $("settings-folder").value = picked; browseTo(picked); }
    } catch (err) { why("settings-error", String(err)); }
  });

  $("adm-save").addEventListener("click", () => sendPassword({
    current: $("adm-now").value, new: $("adm-new").value, confirm: $("adm-confirm").value }));
  $("adm-reset").addEventListener("click", () => sendPassword({ current: $("adm-now").value, reset: true }));

  const checked = () => [...document.querySelectorAll("#dist-items input[data-dist-item]")]
    .filter((box) => box.checked).map((box) => box.dataset.distItem);
  $("dist-export").addEventListener("click", () =>
    sendDistribution("/api/settings/distribution/export", { items: checked() }));
  $("dist-reapply").addEventListener("click", () =>
    sendDistribution("/api/settings/distribution/reapply", {}));
  $("dist-remove").addEventListener("click", () =>
    sendDistribution("/api/settings/distribution/remove", {}));
}
