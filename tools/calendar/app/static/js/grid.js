/*
  grid.js — 月のマス目(曜日見出しと42セル)を描く

  カレンダー画面と履歴画面の**両方がこれを使う。** どの日をどう塗るか・
  何を書くかはサーバが決めていて(`presenters/calendar.py`)、ここは
  返ってきたセルを写すだけ。2つの画面が別々に描くと、片方だけ直したとき
  に同じ月が違って見える。
*/

/** 曜日見出し。 */
export function renderHeaders(container, weekdays) {
  container.replaceChildren(...weekdays.map((day) => {
    const cell = document.createElement("div");
    cell.className = "cal__head";
    if (day.tone) cell.dataset.tone = day.tone;
    cell.textContent = day.label;
    return cell;
  }));
}

/**
 * 6週ぶんのセル。先頭の子(曜日見出しの行)は残して作り直す。
 * **差分は当てない** ── 42セルなら作り直すほうが速く、ずれも起きない
 *
 * @param {HTMLElement} grid
 * @param {object[]} cells サーバが返したセル
 * @param {(cell: object) => void} onClick 当月の日を押したとき
 */
export function renderCells(grid, cells, onClick) {
  while (grid.children.length > 1) grid.lastChild.remove();

  for (let week = 0; week < 6; week += 1) {
    const row = document.createElement("div");
    row.className = "cal__week";
    for (let column = 0; column < 7; column += 1) {
      row.appendChild(dayCell(cells[week * 7 + column], onClick));
    }
    grid.appendChild(row);
  }
}

function dayCell(cell, onClick) {
  // 前後月は押せないので `div`。押せるものだけ `button` にする ──
  // 見た目でなく**要素の種類**で押せるかどうかを表す
  const node = document.createElement(cell.in_month ? "button" : "div");
  node.className = "day";
  node.dataset.tone = cell.tone;
  if (cell.today) node.dataset.today = "1";

  if (cell.in_month) {
    node.type = "button";
    node.dataset.date = cell.date;
    node.setAttribute("aria-label",
      `${cell.day}日${cell.holiday ? " " + cell.holiday : ""}`
      + `${cell.count ? ` 登録${cell.count}件` : ""}`);
    if (cell.tip) node.title = cell.tip;
    node.addEventListener("click", () => onClick(cell));
  }

  const top = document.createElement("div");
  top.className = "day__top";
  if (cell.in_month) {
    const num = document.createElement("span");
    num.className = "day__num";
    num.textContent = String(cell.day);
    top.appendChild(num);
    if (cell.holiday) {
      const name = document.createElement("span");
      name.className = "day__holiday";
      name.textContent = cell.holiday;
      top.appendChild(name);
    }
  }
  node.appendChild(top);

  if (cell.body) {
    const body = document.createElement("div");
    body.className = "day__body";
    body.textContent = cell.body;
    node.appendChild(body);
  }
  return node;
}
