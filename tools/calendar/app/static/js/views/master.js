/*
  views/master.js — マスタ確認・管理

  **直す先は取り込み元だけ。** 手元のDBは総入れ替えで取り込まれるので、
  直しても次の取り込みで消える。書いたあとサーバがその表だけ取り込み
  直すので、画面はその結果を描き直すだけでよい。

  **判断はサーバが持つ。** 直せるか(`editable`)・どの列を打てるか
  (`columns`)・鍵はどれか(`row_key`)・なぜ直せないか(`why`)は
  すべて返ってくる。ここが持つのは「打ち込んだ値を送る」だけ。

  表は1つとは限らない(`tables`)。班員名簿は直せる表、端末一覧は
  **見るだけの表**で、どちらかは `editable` を見て描き分ける ──
  画面の側に「この表は直せない」を書かない。増えたときに必ず食い違う。
*/

import { withPassword } from "../adminpass.js";
import { ApiError, api } from "../api.js";
import { confirm, inform, modal } from "../modal.js";
import { toast, toastError } from "../toast.js";

const el = {};
let state = null;
// いま見ている表。**空なら「サーバの既定に任せる」**
let current = "";
// 並べ替え。**並べるのはサーバ**(画面に出すのは200行までなので、
// 画面の中だけで並べると出ていない行を含めた順にならない)。
// 列名を押すたびに 昇順 → 降順 → 表の既定 と回る
let sort = { column: "", desc: false };

export function start() {
  el.pill = document.getElementById("st-master-pill");
  el.why = document.getElementById("st-master-why");
  el.source = document.getElementById("st-master-source");
  el.host = document.getElementById("st-master-table");
  el.query = document.getElementById("st-master-q");
  el.pick = document.getElementById("st-master-table-pick");
  el.add = document.getElementById("st-master-add");

  document.getElementById("st-master-reload").addEventListener("click", () => load());
  el.add.addEventListener("click", addRow);
  el.query.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); load(); }
  });
  if (el.pick) {
    el.pick.addEventListener("change", () => {
      current = el.pick.value;
      // 表を変えたら絞り込みと並べ替えは外す。前の表の言葉で絞ったまま
      // 「1件もありません」と出ると、空なのか絞れているのか分からない
      el.query.value = "";
      sort = { column: "", desc: false };
      load();
    });
  }

  load();
}

/** 参照パスが変わったら読み直す(設定画面の保存から呼ばれる)。 */
export function refresh() {
  load();
}

/** いまの見え方(絞り込み・並べ替え)。**直したあとも同じ並びで返してもらう** */
function viewOf() {
  return { q: el.query ? el.query.value.trim() : "",
           sort: sort.column, desc: sort.desc };
}

async function load() {
  const view = viewOf();
  const table = current ? `&table=${encodeURIComponent(current)}` : "";
  const order = view.sort
    ? `&sort=${encodeURIComponent(view.sort)}&desc=${view.desc ? 1 : 0}` : "";
  try {
    render(await api.get(`/api/master?q=${encodeURIComponent(view.q)}${table}${order}`));
  } catch (err) {
    toastError(err);
  }
}

function render(payload) {
  state = payload;
  current = payload.table;
  // サーバが受け付けた並びに合わせる(知らない列なら既定に戻っている)
  sort = { column: payload.sort || "", desc: !!payload.desc };
  renderPicker(payload);

  // 直せるかどうかと、その理由。**理由はその場に出す**
  // **「見るだけ」と言い切らない表がある。** 端末一覧は値こそ直せないが、
  // 使わなくなった行は消せる ── 「見るだけ」と出ていると、消せることに
  // 気づかないまま古い行が残り続ける
  el.pill.textContent = payload.editable ? "直せます"
    : (payload.removable ? "消すだけ" : "見るだけ");
  el.pill.className = "sub pill pill--" + (payload.editable ? "ok" : "warn");
  if (payload.why) {
    el.why.textContent = payload.why;
    el.why.hidden = false;
  } else {
    el.why.hidden = true;
  }

  el.source.textContent = payload.source
    ? `取り込み元: ${payload.source}`
    : "取り込み元が決まっていません。";

  // 見るだけの表では**押せないボタンを出さない**。押せない理由を
  // 探させるより、そもそも無いほうが早い
  el.add.hidden = !canAdd(payload);
  el.add.disabled = !payload.editable && !payload.removable;

  if (!payload.columns.length) {
    el.host.replaceChildren(empty("表示できる列がありません。"));
    return;
  }
  if (!payload.rows.length) {
    el.host.replaceChildren(empty(emptyText(payload)));
    return;
  }
  el.host.replaceChildren(table(payload));
}

/** この表に**打てる欄が1つでもある**か。
 *
 * 表ごとの真偽では足りない ── 端末一覧は実績が並ぶ表だが、
 * 「次のライン」だけは打てる(前もって決めておくため)。
 */
function hasEditableColumn(payload) {
  return (payload.columns || []).some((c) => c.editable !== false);
}

/** 足せる表か(打てる欄があり、かつ足す欄がある)。 */
function canAdd(payload) {
  return hasEditableColumn(payload)
    && (payload.columns || []).some((c) => c.at_create !== false);
}

function emptyText(payload) {
  if (payload.query) return "絞り込みに合う行がありません。";
  if (!canAdd(payload)) return "まだ1件もありません。";
  return "1行もありません。[行を追加] から登録できます。";
}

function renderPicker(payload) {
  if (!el.pick) return;
  const tables = payload.tables || [];
  // 1つしか無いなら選ばせる意味が無い
  el.pick.hidden = tables.length < 2;
  if (el.pick.parentElement) el.pick.parentElement.hidden = tables.length < 2;
  if (el.pick.dataset.filled !== String(tables.length)) {
    el.pick.replaceChildren(...tables.map((t) => {
      const option = document.createElement("option");
      option.value = t.table;
      option.textContent = t.label;
      return option;
    }));
    el.pick.dataset.filled = String(tables.length);
  }
  el.pick.value = payload.table;
}

function empty(text) {
  const node = document.createElement("p");
  node.className = "empty";
  node.textContent = text;
  return node;
}

function table(payload) {
  const box = document.createElement("div");
  // **打てる欄と読むだけの欄が混ざる表**では、打てるほうに枠を出す
  // (全部打てる表で枠を出すと、ただの枠だらけになる)
  const mixed = hasEditableColumn(payload)
    && payload.columns.some((c) => c.editable === false);
  box.className = "mtable" + (mixed ? " mtable--mixed" : "");

  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const head = document.createElement("tr");
  payload.columns.forEach((column) => {
    const th = document.createElement("th");
    // **列名を押すと並べ替え。** 押せることが分かるよう、ボタンにする
    // (キーボードでも押せる)。向きは記号と aria-sort の両方で示す
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mtable__sort";
    const active = payload.sort === column.name;
    const mark = active ? (payload.desc ? " ▼" : " ▲") : "";
    button.textContent = column.name + (column.required ? " *" : "") + mark;
    button.title = active
      ? (payload.desc ? "押すと既定の並びに戻します" : "押すと降順にします")
      : "押すとこの列で並べ替えます(昇順)";
    if (active) {
      th.setAttribute("aria-sort", payload.desc ? "descending" : "ascending");
      th.dataset.sorted = "1";
    }
    button.addEventListener("click", () => sortBy(column.name));
    th.appendChild(button);
    head.appendChild(th);
  });
  head.appendChild(document.createElement("th"));
  thead.appendChild(head);
  table.appendChild(thead);

  const body = document.createElement("tbody");
  payload.rows.forEach((row) => body.appendChild(rowNode(payload, row)));
  table.appendChild(body);
  box.appendChild(table);

  if (payload.truncated) {
    const note = document.createElement("p");
    note.className = "hint";
    note.style.padding = "6px 8px";
    // 切ったぶんは**必ず数で言う**
    note.textContent =
      `${payload.total} 件のうち ${payload.limit} 件を表示しています。`
      + "絞り込んでください。";
    box.appendChild(note);
  }
  return box;
}

function rowNode(payload, row) {
  const key = row[payload.row_key];
  const tr = document.createElement("tr");
  const inputs = {};

  // **列ごとに描き分ける。** 打てない欄に枠を出すと「打てるのに
  // 保存できない」ように見えるので、読むだけの欄はただの文字にする
  // (端末一覧は実績が並ぶ中に「次のライン」だけ打てる欄がある)
  payload.columns.forEach((column) => {
    const td = document.createElement("td");
    const writable = column.editable !== false && !column.is_key
      && payload.editable !== false;
    if (!writable) {
      td.textContent = row[column.name] ?? "";
      // 鍵の列だけは、直せる表でも枠のまま出す(これまでどおり) ──
      // 「ここは変えられない」ことが並びで分かる
      if (column.is_key && payload.editable) {
        td.textContent = "";
        const fixed = document.createElement("input");
        fixed.type = "text";
        fixed.value = row[column.name] ?? "";
        fixed.disabled = true;
        fixed.dataset.key = "1";
        td.appendChild(fixed);
      }
      tr.appendChild(td);
      return;
    }
    const input = document.createElement("input");
    input.type = "text";
    input.value = row[column.name] ?? "";
    // **空欄が何を意味するか**を書いておく。端末一覧の「次のライン」は
    // 空が普通の状態なので、何も書かないと使い方が分からない
    if (column.placeholder) input.placeholder = column.placeholder;
    suggest(input, payload.table, column);
    input.addEventListener("input", () => { tr.dataset.dirty = "1"; });
    inputs[column.name] = input;
    td.appendChild(input);
    tr.appendChild(td);
  });

  const act = document.createElement("td");
  act.className = "act";
  if (Object.keys(inputs).length) {
    const save = document.createElement("button");
    save.type = "button";
    save.className = "btn btn--primary";
    save.textContent = "保存";
    // **描いた時点の中身**を一緒に渡す。保存するまでに別の端末が直して
    // いれば、サーバが断る(`calendar_app/master_admin.py`)── 送るのは
    // 打てた列すべてなので、渡さないと自分が触っていない欄まで
    // 古い写しで上書きしてしまう
    const opened = { ...row };
    save.addEventListener("click", () => saveRow(payload, key, inputs, opened));
    act.appendChild(save);
  }
  // **直せなくても消せる表がある**(端末一覧)。入れ替えて使わなく
  // なったPCの行は、誰かが片付けないと残り続ける
  if (payload.removable && key) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn--danger";
    if (act.childElementCount) remove.style.marginLeft = "5px";
    remove.textContent = "削除";
    remove.addEventListener("click", () => deleteRow(payload, key, row));
    act.appendChild(remove);
  }
  tr.appendChild(act);
  return tr;
}

/**
 * 入力候補を付ける(`<datalist>`)。**候補以外も打てる** ── アクセス権限は
 * 別ツールと共用で、ライン名以外の値(mode:field など)も入るため。
 * 候補の出どころはサーバ(`Managed.suggestions`)。
 */
function suggest(input, table, column) {
  if (!column.suggestions || !column.suggestions.length) return;
  const id = `ma-dl-${table}-${column.name}`;
  let list = document.getElementById(id);
  if (!list) {
    list = document.createElement("datalist");
    list.id = id;
    column.suggestions.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      list.appendChild(option);
    });
    document.body.appendChild(list);
  }
  input.setAttribute("list", id);
}

/** 列名を押した。昇順 → 降順 → 表の既定 と回る。 */
async function sortBy(column) {
  // **打ちかけの値を黙って消さない。** 並べ替えは描き直しなので、
  // 保存していない欄の中身は消える
  if (el.host.querySelector("tr[data-dirty]")) {
    const ok = await confirm("保存していない変更があります",
      "並べ替えると、保存していない欄の内容は消えます。並べ替えますか?",
      { okLabel: "並べ替える", danger: true });
    if (!ok) return;
  }
  if (sort.column !== column) sort = { column, desc: false };
  else if (!sort.desc) sort = { column, desc: true };
  else sort = { column: "", desc: false };
  load();
}

/* ------------------------------------------------------------------
   直す
   ------------------------------------------------------------------ */
async function saveRow(payload, key, inputs, opened) {
  const values = {};
  const expected = {};
  // **打てた列だけ送る。** 読むだけの欄まで送ると、サーバは捨てるだけだが
  // 「開いたときの中身」が余計に増えて、他人の更新と食い違いやすくなる
  Object.keys(inputs).forEach((name) => {
    values[name] = inputs[name].value;
    // 開いたときの中身。**これが変わっていたらサーバが断る**
    expected[name] = (opened && opened[name]) ?? "";
  });
  await send("/api/master/save",
             { table: payload.table, key, values, expected });
}

async function deleteRow(payload, key, row) {
  // **取り消せない操作なので、止める確認を残す。**
  // 直す先は共有のマスタで、消えると全員に効く
  const name = row["名前"] || row["PC名"] || key;
  const ok = await confirm("削除の確認",
    `${name}(${payload.row_key} ${key})を取り込み元から削除します。\n`
    + "この操作は共有のマスタに効くため、他の端末にも反映されます。\n"
    // 端末一覧は**その端末がまた起動すれば戻ってくる**。消えたままに
    // なると思われると、消すのをためらう
    + (payload.table === "端末設定"
       ? "その端末が次に起動すると、また一覧に出ます。" : ""),
    { okLabel: "削除する", danger: true });
  if (!ok) return;
  await send("/api/master/delete", { table: payload.table, key });
}

async function addRow() {
  if (!state || !canAdd(state)) return;

  // **足すときに打てる列だけ出す。** 端末一覧なら PC名・ログインID・
  // 次のライン の3つで、鍵(端末キー)はサーバが組み立てる
  const fields = state.columns.filter((c) => c.at_create !== false);
  const values = await modal({
    title: `${state.label}に追加`,
    wide: true,
    render(body) {
      const host = document.createElement("div");
      host.id = "ma-add";
      fields.forEach((column) => {
        const field = document.createElement("div");
        field.className = "field";
        const label = document.createElement("label");
        label.textContent = column.name + (column.required ? " (必須)" : "");
        const input = document.createElement("input");
        input.className = "input";
        input.dataset.name = column.name;
        if (column.placeholder) input.placeholder = column.placeholder;
        suggest(input, state.table, column);
        if (column === fields[0]) input.setAttribute("data-autofocus", "1");
        field.append(label, input);
        host.appendChild(field);
      });
      body.appendChild(host);
    },
    actions: [
      { label: "取り消し", value: null },
      { label: "追加する", kind: "primary",
        value: () => {
          const out = {};
          document.querySelectorAll("#ma-add input").forEach((input) => {
            out[input.dataset.name] = input.value;
          });
          // 必須が空なら閉じない(サーバも断るが、往復させない)
          const missing = fields.some(
            (c) => c.required && !String(out[c.name] || "").trim());
          return missing ? undefined : out;
        } },
    ],
  });
  if (!values) return;
  await send("/api/master/add", { table: state.table, values });
}

async function send(path, body) {
  // 端末のライン予約は**管理者パスワードで守られている**
  // (他の端末の設定を決めるので、その端末で変えるのと同じこと)。
  // 聞き方は `adminpass.js` に1つだけ置いてある
  // 見ていた並び(絞り込み・並べ替え)のまま返してもらう
  const result = await withPassword((sending) => api.post(path, sending),
                                    { ...body, view: viewOf() });
  if (result.ok) {
    render(result.payload);
    toast(result.payload.message, "ok");
    return;
  }
  if (result.cancelled) return;

  const err = result.error;
  if (err instanceof ApiError && err.body && err.body.columns) {
    // 断られても**いまの中身は返ってくる**ので、描き直したうえで理由を出す
    render(err.body);
  }
  if (result.exhausted) {
    await inform("管理者パスワードが確認できません", err.message);
    return;
  }
  if (err instanceof ApiError && (err.status === 422 || err.status === 409)) {
    await inform("直せません", err.message);
    return;
  }
  toastError(err);
}
