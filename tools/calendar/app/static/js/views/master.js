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
import * as leave from "../leave.js";
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

/*
  【打ちかけの行を、ほかの行の保存で消さない】
  表は保存・追加・削除・再読込・絞り込み・表の切り替えのたびに**丸ごと
  描き直す。** 以前は、1行目を直している途中で2行目を保存すると、
  返ってきた表で描き直して**1行目の打ちかけが黙って消えた。**
  「打った値が勝手に戻ることがありました」── とんでもない話である。

  打ちかけの行は鍵ごとに覚えておき(`carried`)、描き直した表の同じ行へ
  戻す。「開いたときの中身」(`opened`)も**最初に開いたときのまま**持ち回す
  ── 描き直しのたびに新しい中身へ取り替えると、そのあいだに別の端末が
  直した値を、こちらの古い打ちかけで黙って上書きしてしまう(サーバが
  「先に直されています」と断れなくなる)。
  消してよいかは利用者が決める: 再読込・絞り込み・表の切り替え・並べ替えは
  打ちかけがあれば先に訊く。
*/
const carried = new Map();
// 行を足す画面に打ちかけた値(送れなかったとき開き直す)
let addDraft = null;

function rowId(table, key) {
  return `${table}\u0000${key}`;
}

/** 打ちかけを消してよいかを訊く。無ければそのまま true。 */
async function mayDiscard(what) {
  if (!carried.size) return true;
  const ok = await confirm("保存していない変更があります",
    `${what}と、保存していない行(${carried.size} 行)の内容は消えます。続けますか?`,
    { okLabel: "消して続ける", cancelLabel: "やめる", danger: true });
  if (ok) carried.clear();
  return ok;
}

export function start() {
  leave.register(() => {
    const parts = [];
    if (carried.size) parts.push(`マスタ管理の ${carried.size} 行`);
    if (addDraft && Object.values(addDraft).some((v) => String(v || "").trim())) {
      parts.push("マスタ管理の「行を追加」");
    }
    return parts.length ? `${parts.join("・")}(まだ保存していません)` : null;
  });

  el.pill = document.getElementById("st-master-pill");
  el.why = document.getElementById("st-master-why");
  el.source = document.getElementById("st-master-source");
  el.host = document.getElementById("st-master-table");
  el.query = document.getElementById("st-master-q");
  el.pick = document.getElementById("st-master-table-pick");
  el.add = document.getElementById("st-master-add");

  document.getElementById("st-master-reload").addEventListener("click", async () => {
    if (!await mayDiscard("読み込み直す")) return;
    load();
  });
  el.add.addEventListener("click", () => addRow());
  el.query.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (!await mayDiscard("絞り込む")) return;
    load();
  });
  if (el.pick) {
    el.pick.addEventListener("change", async () => {
      if (!await mayDiscard("表を切り替える")) {
        el.pick.value = current;            // 選び直しを取り消す
        return;
      }
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

// 読み込みの番号。**後から頼み直したら、前の返事は描かない**
let loadSeq = 0;

async function load() {
  const mine = ++loadSeq;
  const view = viewOf();
  const table = current ? `&table=${encodeURIComponent(current)}` : "";
  const order = view.sort
    ? `&sort=${encodeURIComponent(view.sort)}&desc=${view.desc ? 1 : 0}` : "";
  try {
    const payload = await api.get(`/api/master?q=${encodeURIComponent(view.q)}${table}${order}`);
    if (mine !== loadSeq) return;
    render(payload);
  } catch (err) {
    if (mine === loadSeq) toastError(err);
  }
}

function render(payload) {
  state = payload;
  current = payload.table;
  forgetVanished(payload);
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

/**
 * 打ちかけの行が**取り込み元から無くなっていた**ら、覚えをやめて知らせる。
 * 絞り込み中・切った表示では、見えないだけかもしれないので触らない。
 */
function forgetVanished(payload) {
  if (payload.query || payload.truncated || !payload.row_key) return;
  const present = new Set((payload.rows || []).map(
    (row) => rowId(payload.table, row[payload.row_key])));
  const prefix = `${payload.table}\u0000`;
  const gone = [...carried.keys()].filter((id) => id.startsWith(prefix) && !present.has(id));
  if (!gone.length) return;
  gone.forEach((id) => carried.delete(id));
  toast(`保存していない変更のあった ${gone.length} 行は、取り込み元から無くなっていました`
        + "(別の端末で削除されたなど)。", "warn");
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
  const id = rowId(payload.table, key);
  // 描き直す前に打ちかけていた行。値と「開いたときの中身」を戻す
  const kept = carried.get(id);
  if (kept) tr.dataset.dirty = "1";

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
    input.value = kept && column.name in kept.values
      ? kept.values[column.name] : (row[column.name] ?? "");
    // **空欄が何を意味するか**を書いておく。端末一覧の「次のライン」は
    // 空が普通の状態なので、何も書かないと使い方が分からない
    if (column.placeholder) input.placeholder = column.placeholder;
    suggest(input, payload.table, column);
    input.addEventListener("input", () => {
      tr.dataset.dirty = "1";
      // 打った値を鍵ごとに覚える(ほかの行の保存で描き直されても戻す)
      const values = {};
      Object.keys(inputs).forEach((name) => { values[name] = inputs[name].value; });
      carried.set(id, { values, opened });
    });
    inputs[column.name] = input;
    td.appendChild(input);
    tr.appendChild(td);
  });

  // **描いた時点の中身**。打ちかけを持ち回した行は、最初に開いたときのまま
  const opened = kept ? kept.opened : { ...row };
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
    save.addEventListener("click", () => saveRow(payload, key, inputs, opened, save));
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
  // **打ちかけの値を黙って消さない。** 並べ替えると、保存していない行が
  // 200行の外へ出て見えなくなることがある。先に訊く
  if (!await mayDiscard("並べ替える")) return;
  if (sort.column !== column) sort = { column, desc: false };
  else if (!sort.desc) sort = { column, desc: true };
  else sort = { column: "", desc: false };
  load();
}

/* ------------------------------------------------------------------
   直す
   ------------------------------------------------------------------ */
async function saveRow(payload, key, inputs, opened, button) {
  // **返事が来るまで、この行の「保存」は押せなくする。** ダブルクリックの
  // 2回目が「開いたときの中身」のまま届き、1回目で変わった行と食い違って
  // 「先に直されています」と断られていた(自分の1回目に負ける)
  if (button) {
    if (button.disabled) return;
    button.disabled = true;
  }
  const values = {};
  const expected = {};
  // **打てた列だけ送る。** 読むだけの欄まで送ると、サーバは捨てるだけだが
  // 「開いたときの中身」が余計に増えて、他人の更新と食い違いやすくなる
  Object.keys(inputs).forEach((name) => {
    values[name] = inputs[name].value;
    // 開いたときの中身。**これが変わっていたらサーバが断る**
    expected[name] = (opened && opened[name]) ?? "";
  });
  try {
    await send("/api/master/save",
               { table: payload.table, key, values, expected },
               { saved: rowId(payload.table, key) });
  } finally {
    // 描き直されていれば新しいボタンになっている(古いほうは捨てられる)
    if (button && button.isConnected) button.disabled = false;
  }
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
  await send("/api/master/delete", { table: payload.table, key },
             { saved: rowId(payload.table, key) });
}

async function addRow(prefill = null, note = "") {
  if (!state || !canAdd(state)) return;

  // **足すときに打てる列だけ出す。** 端末一覧なら PC名・ログインID・
  // 次のライン の3つで、鍵(端末キー)はサーバが組み立てる
  const table = state.table;
  const fields = state.columns.filter((c) => c.at_create !== false);
  addDraft = { ...(prefill || {}) };
  const read = () => {
    const out = {};
    document.querySelectorAll("#ma-add input").forEach((input) => {
      out[input.dataset.name] = input.value;
    });
    return out;
  };
  const values = await modal({
    title: `${state.label}に追加`,
    wide: true,
    // 打ちかけがあれば、閉じる前に訊く
    dirty: () => Object.values(read()).some((v) => String(v || "").trim()),
    render(body) {
      if (note) {
        const why = document.createElement("div");
        why.className = "note note--warn";
        why.style.whiteSpace = "pre-wrap";
        why.textContent = note;
        body.appendChild(why);
      }
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
        if (prefill && column.name in prefill) input.value = prefill[column.name];
        if (column.placeholder) input.placeholder = column.placeholder;
        input.addEventListener("input", () => { addDraft = read(); });
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
          const out = read();
          // 必須が空なら閉じない(サーバも断るが、往復させない)
          const missing = fields.some(
            (c) => c.required && !String(out[c.name] || "").trim());
          return missing ? undefined : out;
        } },
    ],
  });
  if (!values) { addDraft = null; return; }
  addDraft = values;
  const result = await send("/api/master/add", { table, values }, { quiet: true });
  if (result.ok) { addDraft = null; return; }
  // **断られても打った値は捨てない。** 理由を上に出して、同じ値で開き直す
  // (以前は 409/422・パスワードの取り消しで、打った行がまるごと消えていた)
  const err = result.error;
  const why = result.cancelled
    ? "管理者パスワードを取り消したので、まだ追加していません。追加するには、もう一度「追加する」を押してください。"
    : (err && err.message ? err.message : String(err));
  if (state && state.table === table) await addRow(values, why);
  else addDraft = null;
}

/**
 * @param {{saved?: string, quiet?: boolean}} [opts]
 *   saved … 通ったら打ちかけの覚えを外す行 / quiet … 断りを出さない(呼んだ側が出す)
 * @returns {Promise<{ok:boolean, error?:any, cancelled?:boolean}>}
 */
async function send(path, body, opts = {}) {
  // 端末のライン予約は**管理者パスワードで守られている**
  // (他の端末の設定を決めるので、その端末で変えるのと同じこと)。
  // 聞き方は `adminpass.js` に1つだけ置いてある
  // 見ていた並び(絞り込み・並べ替え)のまま返してもらう
  const result = await withPassword((sending) => api.post(path, sending),
                                    { ...body, view: viewOf() });
  if (result.ok) {
    // 保存した行の覚えだけを外す。**ほかの行の打ちかけは描き直したあとに戻る**
    if (opts.saved) carried.delete(opts.saved);
    loadSeq += 1;                    // これより前の読み込みの返事は捨てる
    render(result.payload);
    toast(result.payload.message, "ok");
    return result;
  }
  if (result.cancelled) return result;

  const err = result.error;
  if (err instanceof ApiError && err.body && err.body.columns) {
    // 断られても**いまの中身は返ってくる**ので、描き直したうえで理由を出す
    // (打ちかけの行は、描き直したあとに戻る)
    loadSeq += 1;
    render(err.body);
  }
  if (opts.quiet) return result;
  if (result.exhausted) {
    await inform("管理者パスワードが確認できません", err.message);
    return result;
  }
  if (err instanceof ApiError && (err.status === 422 || err.status === 409)) {
    await inform("直せません", err.message);
    return result;
  }
  toastError(err);
  return result;
}
