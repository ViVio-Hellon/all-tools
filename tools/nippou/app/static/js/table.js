/*
  table.js — サーバが組み立てた表(`presenters/agg_admin.Table`)を器へ流し込む

  **列も丸め方もサーバが決めます。** ここは受け取った行を並べるだけで、
  集計管理(`views/agg.js`)と標準作業時間(`views/standard_time.js`)の
  両方が使う ── 同じ表を2か所で描かない。
*/

/**
 * 表1つを描き直す。`numeric` が真の列だけ右寄せにする。
 *
 * 器は `[data-table="<key>"]` のカード。中の `[data-table-note]` に
 * 題の注記を、`[data-table-body]` に表を入れます。
 *
 * `rowAction` を渡すと、行の頭にボタンを1つ置きます
 * (`{ label, title(i), onClick(i) }` ── `i` は行の並びの番号)。
 */
export function paintTable(table, { rowAction = null } = {}) {
  const card = document.querySelector(`[data-table="${table.key}"]`);
  if (!card) return;

  const note = card.querySelector("[data-table-note]");
  if (note) note.textContent = `${table.note}（${table.count}行）`;

  const body = card.querySelector("[data-table-body]");
  if (!body) return;
  body.replaceChildren();

  if (!table.rows.length) {
    const empty = document.createElement("p");
    empty.className = "lead";
    empty.textContent = table.empty;
    body.append(empty);
    return;
  }

  const node = document.createElement("table");
  node.className = "list list--nowrap";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  if (rowAction) headRow.append(document.createElement("th"));
  table.columns.forEach((label, index) => {
    const th = document.createElement("th");
    if (table.numeric[index]) th.className = "num";
    th.textContent = label;
    headRow.append(th);
  });
  head.append(headRow);

  const tbody = document.createElement("tbody");
  table.rows.forEach((row, rowIndex) => {
    const tr = document.createElement("tr");
    if (rowAction) {
      const td = document.createElement("td");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn--sm";
      btn.textContent = rowAction.label;
      if (rowAction.title) btn.title = rowAction.title(rowIndex);
      btn.addEventListener("click", () => rowAction.onClick(rowIndex));
      td.append(btn);
      tr.append(td);
    }
    row.forEach((cell, index) => {
      const td = document.createElement("td");
      if (table.numeric[index]) td.className = "num";
      td.textContent = cell;
      tr.append(td);
    });
    tbody.append(tr);
  });
  node.append(head, tbody);
  body.append(node);
}
