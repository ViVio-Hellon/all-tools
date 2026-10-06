/*
  views/master.js — マスタ管理(表を見る / 直す)

  **相手は sqlite3 です。** Access なら現場のPCで開いて直せましたが、
  sqlite3 は開くための道具が入っていない前提で考えるほかありません
  (テキストエディタでも開けないバイナリ形式です)。確かめる場所と
  直す場所を、このツールの中で完結させます。

  【見た目の作り ── 一覧(左)と行(右)】
  選択欄を2つ(ファイル・表)並べていたときは、**開くまで何があるか
  分かりませんでした。** 左に一覧を出しておくと、選ぶ前に「どれが
  直せて、どれが見るだけか」が読めます。**直せないものも出します**
  ── 隠すと「あるはずの表が無い」に見えます。

  【判断はサーバ】
  直せるか(`page.editable`)・鍵が開いているか(`unlocked`)・直せない
  理由(`page.why`)は、どれもサーバが決めます。ここは受け取った状態を
  写すだけで、画面が自分で判断しません ── 同じ規則が2か所に散ると、
  片方だけ直したときに食い違います。

  【書き先は元のファイル】
  読むときは手元の写しを開きますが(共有に触らないため)、**書くときは
  元のファイルへ直に書きます** ── 写しを書き換えても誰にも届きません。
  だから直した内容は他の端末にも効きます。
*/
import { api } from "../api.js";
import { toast, toastError } from "../toast.js";
import { onLeave } from "../nav.js";
import { open as openModal, close as closeModal } from "./modals.js";
import { watch as watchJob } from "../progress.js";

const byId = (id) => document.getElementById(id);
const val = (id) => byId(id)?.value ?? "";

function show(id, on) {
  const el = byId(id);
  if (el) el.hidden = !on;
}

/** 表ぜんぶの点検(アクセス権限・班員名簿のオペレーター)。**出している行だけでなく全部**
 *  をサーバが見た結果。見出しもサーバが決める(表ごとに、何に効かないかが違う)。 */
function paintChecks(checks, title = "") {
  const box = document.getElementById("tbl-checks");
  if (!box) return;
  box.hidden = !checks.length;
  box.replaceChildren();
  if (!checks.length) return;
  const head = document.createElement("b");
  head.textContent = title || `読めない行が${checks.length}行あります`;
  const list = document.createElement("ul");
  list.className = "check__list";
  for (const text of checks) {
    const li = document.createElement("li");
    li.textContent = text;
    list.append(li);
  }
  box.append(head, list);
}

function setText(id, value) {
  const el = byId(id);
  if (el) el.textContent = value || "";
}

let state = null;       // サーバが返した、まるごとの状態
let editing = null;     // いま直している行(足すときは null)
let findTimer = null;

function note(text, kind = "info") {
  const box = byId("table-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

/** サーバから、まるごとの状態を引き直す。 */
async function load(over = {}) {
  const file = over.file ?? (state?.file || "");
  const table = over.table ?? (over.file ? "" : (state?.table || ""));
  const query = over.q ?? val("tbl-q");
  const sort = over.sort ?? (state?.page?.sort || "");
  const dir = over.sort_dir ?? (state?.page?.sort_dir || "asc");
  try {
    paint(await api.get(
      "/api/master/browse"
      + `?file=${encodeURIComponent(file)}`
      + `&table=${encodeURIComponent(table)}`
      + `&q=${encodeURIComponent(query)}`
      + `&sort=${encodeURIComponent(sort)}`
      + `&sort_dir=${encodeURIComponent(dir)}`));
  } catch (err) { toastError(err); }
}

/** 書いたあとの応答。**まるごと返ってくる**ので、そのまま写す。 */
function paint(body) {
  if (!body) return;
  state = body;

  paintList(body);

  const q = byId("tbl-q");
  if (q && q !== document.activeElement) q.value = body.query || "";

  const page = body.page || {};
  const editable = !!page.editable;
  // **足す・消すができない表**(決まったキーの行だけの表)では出さない
  show("tbl-add", editable && page.can_add !== false);
  // 列を足す(v4.14.0)。直せる表で鍵が開いているときだけ出す。直せる表なのに
  // 足せない(VC計算マスタ)ときは、押せない形で理由を添える
  const addCol = byId("tbl-add-col");
  if (addCol) {
    addCol.hidden = !editable;
    addCol.disabled = !page.can_add_column;
    addCol.title = page.can_add_column ? "" : (page.add_column_why || "");
  }
  const added = page.added_columns || [];
  setText("tbl-added", added.length
    ? `マスタ管理で足した列: ${added.join("・")}(見る・直すだけで、計算・帳票には使われません)`
    : "");
  show("tbl-added", added.length > 0);
  setText("tbl-count", page.total ? `${page.shown}/${page.total}件` : "");
  // いまの並び。**押せることを言う**(見出しが押せると気づかない)
  setText("tbl-sort", page.columns && page.columns.length
    ? (page.sort
      ? `並び: ${page.sort} の${page.sort_dir === "desc" ? "大きい順 ▼" : "小さい順 ▲"}`
        + "(見出しをもう一度押すと逆・3回目で元の並び)"
      : "並び: 表の順(見出しを押すと、その列で並び替え)")
    : "");
  // **直せない理由は先に言う。** 押してから断られるのは手戻り
  setText("tbl-why", [page.why, page.note].filter(Boolean).join(" "));
  paintChecks(page.checks || [], page.checks_head || "");
  setText("tbl-source", page.source
    ? (editable
        ? `書き先は ${page.source} です。手元の写しではなく元のファイルを`
          + "直すので、他の端末にも効きます。"
        : `読んでいるのは ${page.source} です。`)
    : "");
  if (page.error) note(page.error, "warn");
  else if (body.message) note(body.message, "ok");
  else note("");

  paintGrid(page, editable);
}

/* ---- 左の一覧 ----------------------------------------------------
   ファイルごとにまとめ、**直せるものを上に**出す(並びはサーバが決めた
   もの)。直せないものには、その場に理由を書く。 */
function paintList(body) {
  const host = byId("m-list");
  if (!host) return;
  // **一覧の流れた位置を残す。** 描き直すと中身が一度空になり、位置が
  // 頭へ戻ります ── 下のほうの表を選ぶたびに一覧が先頭へ跳ねると、
  // 隣の表を続けて見られません
  const kept = host.scrollTop;
  host.replaceChildren();

  for (const group of body.catalog || []) {
    const head = document.createElement("p");
    head.className = "mlist__group";
    head.textContent = group.editable
      ? `${group.label}(直せます)`
      : `${group.label}(見るだけ)`;
    host.append(head);

    if (group.why) {
      const why = document.createElement("p");
      why.className = "mitem__note";
      why.style.padding = "0 var(--sp-2)";
      why.textContent = group.why;
      host.append(why);
    }
    if (!group.tables.length) {
      const empty = document.createElement("p");
      empty.className = "mitem__note";
      empty.style.padding = "0 var(--sp-2)";
      empty.textContent = group.error
        || (group.kind === "sqlite3" ? "表がありません。"
                                     : "Access なので中身を出せません。");
      host.append(empty);
      continue;
    }

    for (const entry of group.tables) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mitem";
      btn.dataset.editable = group.editable ? "1" : "0";
      if (group.file === body.file && entry.table === body.table) {
        btn.setAttribute("aria-current", "true");
      }
      const mark = document.createElement("span");
      mark.className = "mitem__mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = group.mark;
      const bodyEl = document.createElement("span");
      bodyEl.className = "mitem__body";
      const name = document.createElement("span");
      name.className = "mitem__name";
      name.textContent = entry.table;
      bodyEl.append(name);
      if (entry.note) {
        const memo = document.createElement("span");
        memo.className = "mitem__note";
        memo.textContent = entry.note;
        bodyEl.append(memo);
      }
      btn.append(mark, bodyEl);
      btn.addEventListener("click", async () => {
        // 表を替えたら並びは元に(前の表で押した列を持ち越さない)
        await load({ file: group.file, table: entry.table, q: "", sort: "", sort_dir: "asc" });
        revealTable();
      });
      host.append(btn);
    }
  }
  if (!(body.catalog || []).length) {
    const empty = document.createElement("p");
    empty.className = "lead";
    empty.textContent = "読めるファイルがありません。参照用マスタの置き場所を確かめてください。";
    host.append(empty);
  }
  host.scrollTop = kept;
  keepInList(host.querySelector('.mitem[aria-current="true"]'), host);
}

/** 選んでいる表が一覧の枠の外なら、**一覧の中だけ**を流して見せる。
 *  (`scrollIntoView` は作業面まで動かすので使わない) */
function keepInList(item, host) {
  if (!item || host.scrollHeight <= host.clientHeight) return;
  const top = item.offsetTop;              // `.mlist` は position:relative
  const bottom = top + item.offsetHeight;
  if (top < host.scrollTop) host.scrollTop = top;
  else if (bottom > host.scrollTop + host.clientHeight) {
    host.scrollTop = bottom - host.clientHeight;
  }
}

/** 表を選んだら、右の中身の頭が見えるところまで作業面を戻す。
 *  **見えていれば動かさない**(読んでいる位置を奪わない)。 */
function revealTable() {
  const head = document.querySelector(".masterwrap .mbar");
  const work = byId("main");
  if (!head || !work) return;
  const top = head.getBoundingClientRect().top;
  const view = work.getBoundingClientRect();
  if (top < view.top || top > view.bottom - 160) {
    head.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function paintGrid(page, editable) {
  const head = byId("tbl-head");
  const tbody = byId("tbl-body");
  if (!head || !tbody) return;
  head.replaceChildren();
  tbody.replaceChildren();

  const columns = page.columns || [];
  if (!columns.length) {
    const cell = tbody.insertRow().insertCell();
    cell.className = "empty";
    cell.textContent = page.error || "出せる表がありません。";
    return;
  }

  for (const col of columns) {
    const th = document.createElement("th");
    th.textContent = col;
    th.scope = "col";
    // 見出しを押すと並び替え(全件をサーバが並べる)。押すたびに
    // ▲小さい順 → ▼大きい順 → 元の並び(表の順)。いま押している列には向きを出す
    const on = page.sort === col;
    const desc = on && page.sort_dir === "desc";
    th.dataset.sortable = "1";
    if (on) th.dataset.sort = page.sort_dir;
    th.setAttribute("aria-sort", on ? (desc ? "descending" : "ascending") : "none");
    th.title = !on ? `押すと「${col}」の小さい順(▲)に並び替え`
      : desc ? "押すと元の並び(表の順)に戻す"
        : `押すと「${col}」の大きい順(▼)`;
    th.tabIndex = 0;
    const go = () => {
      if (!on) load({ sort: col, sort_dir: "asc" });
      else if (!desc) load({ sort: col, sort_dir: "desc" });
      else load({ sort: "", sort_dir: "asc" });
    };
    th.addEventListener("click", go);
    th.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); go(); }
    });
    head.append(th);
  }
  if (editable) {
    const th = document.createElement("th");
    th.textContent = "";
    head.append(th);
  }

  for (const row of page.rows || []) {
    const tr = tbody.insertRow();
    for (const col of columns) tr.insertCell().textContent = row[col] ?? "";
    if (editable) {
      const cell = tr.insertCell();
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn--sm";
      btn.textContent = "直す";
      btn.addEventListener("click", () => openRow(row));
      cell.append(btn);
    }
  }
  if (!(page.rows || []).length) {
    const cell = tbody.insertRow().insertCell();
    cell.colSpan = columns.length + (editable ? 1 : 0);
    cell.className = "empty";
    cell.textContent = "行がありません。";
  }
}

/* ---- 1行を直す / 足す ----
   横に並んだ表のままでは、どの欄に何を入れるのか読めません。
   1行ぶんを縦に開きます。 */
function openRow(row) {
  if (!state) return;
  editing = row;
  const host = byId("row-fields");
  if (!host) return;
  host.replaceChildren();
  for (const col of state.columns || []) {
    const label = document.createElement("label");
    label.className = "stack-field";
    const name = document.createElement("span");
    name.className = "stack-field__label";
    name.textContent = col.name + (col.required ? " *" : "");
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.col = col.name;
    input.value = row ? (row[col.name] ?? "") : "";
    const kind = document.createElement("span");
    kind.className = "stack-field__unit";
    kind.textContent = col.kind_label;
    // マスタ管理で足した列(v4.14.0)。計算・帳票には使われないことを、直す場所で言う
    if (col.added) {
      name.title = col.note || "";
      name.textContent += " +";
    }
    label.append(name, input, kind);
    host.append(label);
  }
  setText("row-title", row ? "行を直す" : "1行足す");
  setText("row-why",
       `${state.file} / ${state.table}`
       + (row ? "" : " に新しい行を足します")
       + "。* は空にできません。");
  show("row-delete", !!row && state?.page?.can_delete !== false);
  rowError("");
  openModal("row-modal");
}

function rowError(message) {
  const box = byId("row-error");
  if (!box) return;
  box.hidden = !message;
  box.textContent = message || "";
}

function rowValues() {
  const out = {};
  for (const el of document.querySelectorAll("#row-fields [data-col]")) {
    out[el.dataset.col] = el.value;
  }
  return out;
}

function rowBody(extra = {}) {
  return {
    file: state?.file || "", table: state?.table || "",
    q: val("tbl-q"),
    sort: state?.page?.sort || "", sort_dir: state?.page?.sort_dir || "asc",
    ...extra,
  };
}

/* ---- 直の境界時刻の表を描き直す ----
   時間用を直すと、サーバがその場で手元の直の時刻を写し直し、写した
   ものを返します(`master_admin.after_write`)。同じ画面の「直の境界時刻」
   の面はページを描いたときの値のままなので、**返ってきた値で描き直す**
   ── でないと、直した直後にその面を開いた人が古い時刻を見ます。 */
function paintShiftTimes(rows) {
  const body = byId("shift-times-body");
  if (!body || !Array.isArray(rows)) return;
  body.replaceChildren();
  for (const [key, start, end] of rows) {
    const tr = body.insertRow();
    for (const text of [key, start, end]) tr.insertCell().textContent = text ?? "";
  }
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = 3;
    cell.className = "empty";
    cell.textContent = "既定値で動いています(未取り込み)。";
  }
}

async function writeRow(url, extra) {
  // **進み具合を出す**([1/2] 元のファイルに書く → [2/2] 読み直す)。
  // 書く先は共有の元のファイルなので、遅い日は待たされる(`progress.js`)
  const stop = watchJob("マスタに書いています");
  try {
    const body = await api.post(url, rowBody(extra));
    paint(body);
    paintShiftTimes(body.shift_times);
    closeModal("row-modal");
    toast(state?.message || "書き込みました", "ok");
  } catch (err) {
    // 断りの中身もまるごと返ってくる。**画面は最新の状態になる**
    if (err.body?.page) paint(err.body);
    rowError(err.body?.error?.message || err.message);
  } finally {
    stop();
  }
}

/* ---- 列を足す(v4.14.0)----
   **戻せない操作**なので、押す前に1回だけ何をするかを言う。断られたら窓の中に
   理由を出し、打った名前は残す(窓は閉じない)。足せるか・名前を断るかは
   サーバが決める。ここで見るのは名前が空かどうかだけ。 */
function colError(message) {
  const box = byId("col-error");
  if (!box) return;
  box.hidden = !message;
  box.textContent = message || "";
}

function openAddColumn() {
  const page = state?.page || {};
  if (!state?.table || !page.can_add_column) return;
  setText("col-title", `${state.table} に列を足す`);
  setText("col-note", page.add_column_note || "");
  const name = byId("col-name");
  if (name) name.value = "";
  const kind = byId("col-kind");
  if (kind) kind.value = "text";
  const initial = byId("col-initial");
  if (initial) initial.value = "";
  colError("");
  openModal("col-modal");
  name?.focus();
}

async function addColumn() {
  const table = state?.table;
  if (!table) return;
  const name = val("col-name").trim();
  if (!name) {
    colError("列の名前を入れてください。");
    byId("col-name")?.focus();
    return;
  }
  const kindEl = byId("col-kind");
  const kind = kindEl?.value || "text";
  const kindLabel = kindEl?.selectedOptions?.[0]?.textContent || "文字";
  const initial = val("col-initial").trim();
  const total = state?.page?.total || 0;
  if (!confirm(`${table} に列「${name}」(${kindLabel})を足します。\n`
               + (initial ? `いまある ${total}行 に「${initial}」を入れます。\n` : "")
               + "足した列は消せません。よろしいですか?")) return;
  const save = byId("col-save");
  if (save) save.disabled = true;
  const stop = watchJob("マスタに列を足しています");
  try {
    const body = await api.post("/api/master/column/add",
                                rowBody({ name, kind, initial }));
    paint(body);
    closeModal("col-modal");
    toast(state?.message || "列を足しました", "ok");
  } catch (err) {
    if (err.body?.page) paint({ ...err.body, message: "" });
    colError(err.body?.error?.message || err.message);
  } finally {
    stop();
    if (save) save.disabled = false;
  }
}

export function start() {
  if (!byId("m-list")) return;      // この画面ではない
  state = null;
  editing = null;
  load({ file: "", table: "", q: "", sort: "", sort_dir: "asc" });

  // 「ファイルの状態」の面から「表を見る」で飛んでくる
  for (const btn of document.querySelectorAll("[data-master]")) {
    btn.addEventListener("click", () => {
      load({ file: btn.dataset.master, table: "", q: "", sort: "", sort_dir: "asc" });
      // 面を切り替えないと、読み込んだ先が見えない
      const bar = document.querySelector('[data-tabs="settings.master"]');
      bar?.querySelector('.tab[data-key="tables"]')?.click();
    });
  }

  byId("tbl-reload")?.addEventListener("click", () => load({}));
  // 絞り込みは打ち終わってから引く(1文字ごとに共有を読みに行かない)
  byId("tbl-q")?.addEventListener("input", () => {
    clearTimeout(findTimer);
    findTimer = setTimeout(() => load({}), 250);
  });
  onLeave(() => clearTimeout(findTimer));

  byId("tbl-add")?.addEventListener("click", () => openRow(null));
  byId("row-save")?.addEventListener("click", () => {
    if (editing) {
      writeRow("/api/master/row/save",
               { key: editing[state.row_key], values: rowValues() });
    } else {
      writeRow("/api/master/row/add", { values: rowValues() });
    }
  });
  byId("row-delete")?.addEventListener("click", () => {
    if (!editing) return;
    // **取り返しがつかない操作なので確かめる**(共有のマスタが1行減る)
    if (!confirm("この行を消します。よろしいですか?")) return;
    writeRow("/api/master/row/delete", { key: editing[state.row_key] });
  });
  for (const btn of document.querySelectorAll('[data-close="row-modal"]')) {
    btn.addEventListener("click", () => closeModal("row-modal"));
  }

  byId("tbl-add-col")?.addEventListener("click", openAddColumn);
  byId("col-save")?.addEventListener("click", addColumn);
  byId("col-name")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); addColumn(); }
  });
  for (const btn of document.querySelectorAll('[data-close="col-modal"]')) {
    btn.addEventListener("click", () => closeModal("col-modal"));
  }
}
