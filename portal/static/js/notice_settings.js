/*
  notice_settings.js — 大設定の「案内表示」(1.5.104)

    日報の機能ではなく all-tools 全体の機能として扱って
    大設定で 案内表示マスタの編集をできるようにし …

  共有の DB の表「案内表示」を、ここで足す・直す・消す(鍵を開けた人だけ)。行を直すとき、欄の下に
  説明と**選べる値**(色: 注意 / 警告 / 案内 など)を出し、欄の候補にも入れる。選べる値以外はサーバが
  断る(`guide_store.check_values`)。この端末のライン(案内に使う)もここで決める。
  下に「案内の確認」(14日の押した/押されていない)。
*/
import { api } from "./api.js";
import { toast, toastError } from "./toast.js";

const $ = (id) => document.getElementById(id);
let view = null;        // `/api/notice/settings` の答え
let admin = false;      // 鍵が開いているか(大設定の画面が教える)
let editing = null;     // 直している行(足すときは null)

function rowKey(row) { return row[view.row_key]; }

function setAdminButtons() {
  for (const node of document.querySelectorAll("[data-notice-admin]")) {
    node.setAttribute("aria-disabled", admin ? "false" : "true");
    node.title = admin ? "" : "管理者パスワードで鍵を開けると使えます";
  }
}

function locked() {
  if (admin) return false;
  toast("管理者パスワードで鍵を開けてください", "ng");
  $("lock-pass")?.focus();
  return true;
}

function paint() {
  if (!view) return;
  const line = view.line || {};
  $("notice-line").textContent = line.line
    ? `${line.line}(${line.origin}から)`
    : `決まっていません ── ${line.why || ""}`;
  const choice = $("notice-line-choice");
  choice.replaceChildren(new Option("(アクセス権限の表・日報の設定から)", ""),
                         ...(view.lines || []).map((n) => new Option(n, n)));
  if (line.setting && ![...choice.options].some((o) => o.value === line.setting)) {
    choice.append(new Option(line.setting, line.setting));
  }
  choice.value = line.setting || "";
  $("notice-times").textContent =
    Object.entries(view.times || {}).map(([k, v]) => `${k} ${v}`).join(" / ") + `(${view.times_from || ""})`;
  $("notice-records").textContent = view.records || "";
  const notes = [view.error,
    view.old_table ? `${view.old_table} にも「案内表示」の表がありますが、前の版(日報の中の案内表示)の表で、`
      + "いまは使っていません。案内はこの大設定の表で直してください(日報のマスタ管理で直しても効きません)。"
      + "前の版の行を使うときは「前の版の表から写す」(この表に無い行だけ足します)。" : ""]
    .filter(Boolean);
  $("notice-import").hidden = !view.old_table;
  $("notice-error").hidden = !notes.length;
  $("notice-error").textContent = notes.join(" ");
  const problems = $("notice-problems");
  problems.hidden = !(view.problems || []).length;
  problems.replaceChildren(...(view.problems || []).map((t) => {
    const li = document.createElement("li");
    li.textContent = `出せない案内: ${t}`;
    return li;
  }));
  const body = $("notice-grid").querySelector("tbody");
  const rows = view.rows || [];
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 10;
    td.className = "empty";
    td.textContent = "案内はまだありません(「+ 行を足す」)";
    tr.append(td);
    body.replaceChildren(tr);
  } else {
    body.replaceChildren(...rows.map((row) => {
      const tr = document.createElement("tr");
      tr.dataset.key = rowKey(row);
      for (const name of ["ID", "有効", "ライン", "直", "曜日", "タイミング", "大きさ", "色", "表示文"]) {
        const td = document.createElement("td");
        td.textContent = String(row[name] ?? "");
        tr.append(td);
      }
      const td = document.createElement("td");
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "btn btn--small";
      edit.textContent = "直す";
      edit.dataset.noticeAdmin = "";
      edit.addEventListener("click", () => open(row));
      td.append(edit);
      tr.append(td);
      return tr;
    }));
  }
  setAdminButtons();
}

async function paintChecks() {
  const body = $("notice-checks")?.querySelector("tbody");
  if (!body) return;
  let got;
  try {
    got = await api.get("/api/notice/checks");
  } catch (err) {
    return;
  }
  const missed = $("notice-missed");
  missed.hidden = !got.missed;
  // どのラインのぶんか(1.5.113 ──「押されていない案内: どのラインか出してください」)
  const where = got.line?.line || (got.rows || [])[0]?.line || "";
  missed.textContent = got.missed
    ? `押されていない案内が ${got.missed}件 あります(ライン: ${where || "決まっていません"}・赤い行)。あとの直で出ています`
    : "";
  const rows = got.rows || [];
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "empty";
    td.textContent = got.line?.line ? "この14日に出した案内はありません" : "この端末のラインが決まっていません";
    tr.append(td);
    body.replaceChildren(tr);
    return;
  }
  body.replaceChildren(...rows.map((r) => {
    const tr = document.createElement("tr");
    if (!r.acked && !r.now) tr.className = "is-bad";
    const status = r.acked ? (r.worker || "(名前なし)") : (r.now ? "まだ(いまの直)" : "押されていません");
    for (const text of [r.report_date, r.shift, r.line || "", r.notice, status, r.acked ? r.acked_at : "",
                        r.acked ? r.acked_shift : ""]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    return tr;
  }));
}

export async function refresh(isAdmin = admin) {
  admin = Boolean(isAdmin);
  try {
    view = await api.get("/api/notice/settings");
    paint();
  } catch (err) {
    $("notice-error").hidden = false;
    $("notice-error").textContent = err.message;
  }
  paintChecks();
}

/** 大設定が鍵の開け閉めを知らせる */
export function adminChanged(isAdmin) {
  admin = Boolean(isAdmin);
  setAdminButtons();
}

function open(row) {
  if (locked()) return;
  editing = row;
  const host = $("notice-fields");
  host.replaceChildren();
  for (const col of view.columns || []) {
    const label = document.createElement("label");
    label.className = "notice-field";
    const name = document.createElement("span");
    name.textContent = col.name + (col.name === "表示文" ? " *" : "");
    const input = document.createElement(col.name === "表示文" || col.name === "確認の文" ? "textarea" : "input");
    if (input.tagName === "INPUT") input.type = "text";
    else input.rows = 2;
    input.dataset.col = col.name;
    input.value = row ? String(row[col.name] ?? "") : "";
    label.append(name, input);
    if (col.choices && col.choices.length) {
      const list = document.createElement("datalist");
      list.id = `notice-choices-${col.name}`;
      for (const v of col.choices) list.append(new Option(v, v));
      input.setAttribute("list", list.id);
      label.append(list);
    }
    if (col.hint) {
      const hint = document.createElement("small");
      hint.className = "notice-field__hint";
      hint.dataset.hintFor = col.name;
      hint.textContent = (col.choices && col.choices.length
        ? `選べる値: ${col.choices.join(" / ")} ── ` : "") + col.hint;
      label.append(hint);
    }
    host.append(label);
  }
  $("notice-title").textContent = row ? `案内表示の行を直す(${row.ID || ""})` : "案内表示に1行足す";
  $("notice-delete").hidden = !row;
  $("notice-form-error").hidden = true;
  $("notice-dialog").showModal();
}

function values() {
  const out = {};
  for (const input of $("notice-fields").querySelectorAll("[data-col]")) out[input.dataset.col] = input.value;
  return out;
}

function was(row) {
  const out = {};
  for (const col of view.columns || []) out[col.name] = row[col.name] ?? "";
  return out;
}

async function send(path, payload, done) {
  try {
    const body = await api.post(path, payload);
    view = body;
    paint();
    paintChecks();
    $("notice-dialog").close();
    toast(body.message || done);
  } catch (err) {
    if (err.body && err.body.rows) { view = err.body; paint(); }
    $("notice-form-error").hidden = false;
    $("notice-form-error").textContent = err.message;
  }
}

export function install() {
  $("notice-reload")?.addEventListener("click", () => refresh().then(() => toast("読み直しました")));
  $("notice-add")?.addEventListener("click", () => open(null));
  $("notice-cancel")?.addEventListener("click", () => $("notice-dialog").close());
  $("notice-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (locked()) return;
    if (editing) {
      send("/api/notice/save", { key: rowKey(editing), values: values(), was: was(editing) }, "直しました");
    } else {
      send("/api/notice/add", { values: values() }, "足しました");
    }
  });
  $("notice-delete")?.addEventListener("click", () => {
    if (!editing || locked()) return;
    if (!$("notice-delete").dataset.sure) {
      $("notice-delete").dataset.sure = "1";
      $("notice-delete").textContent = "もう一度押すと消します";
      return;
    }
    delete $("notice-delete").dataset.sure;
    $("notice-delete").textContent = "消す";
    send("/api/notice/delete", { key: rowKey(editing), was: was(editing) }, "消しました");
  });
  $("notice-dialog")?.addEventListener("close", () => {
    delete $("notice-delete").dataset.sure;
    $("notice-delete").textContent = "消す";
  });
  $("notice-import")?.addEventListener("click", async () => {
    if (locked()) return;
    try {
      const body = await api.post("/api/notice/import-old", {});
      view = body;
      paint();
      paintChecks();
      toast(body.message);
    } catch (err) { toastError(err); }
  });
  $("notice-line-save")?.addEventListener("click", async () => {
    if (locked()) return;
    try {
      const body = await api.post("/api/notice/line", { line: $("notice-line-choice").value });
      view = body;
      paint();
      paintChecks();
      toast(body.message);
    } catch (err) { toastError(err); }
  });
}
