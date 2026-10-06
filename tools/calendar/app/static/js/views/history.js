/*
  views/history.js — 履歴表示(過去の月を見るだけ)

  **書き込む道を1つも持たない。** 登録・削除の関数も、書き込む API への
  呼び出しもここには無い ── 昔の月を調べている途中の押し間違いで、
  過去の記録が変わらないようにするため(`app/routes/history.py`)。

  選べる月・どの日をどう塗るかはサーバが決める。ここは写すだけ。
*/

import { openWindow } from "../desktop.js";
import { ApiError, api, tokenUrl } from "../api.js";
import { renderCells, renderHeaders } from "../grid.js";
import { inform, modal } from "../modal.js";
import * as sync from "../sync.js";
import { toastError } from "../toast.js";

const el = {};
let choices = null;           // 選べる月(年ごとに12マス)
let view = null;              // いま出している月
let drawnReceived = null;     // 描いた時点の「取り込み直した時刻」

export function start() {
  el.grid = document.getElementById("his-grid");
  el.headers = document.getElementById("his-headers");
  el.title = document.getElementById("his-title");
  el.empty = document.getElementById("his-empty");
  el.prev = document.getElementById("his-prev");
  el.next = document.getElementById("his-next");
  el.pick = document.getElementById("his-pick");
  el.print = document.getElementById("his-print");

  el.prev.addEventListener("click", () => move(-1));
  el.next.addEventListener("click", () => move(1));
  el.pick.addEventListener("click", pickMonth);
  el.print.addEventListener("click", openPrint);

  // 過去の月でも、他の端末が消した・直したものは取り込み直しで入る。
  // **古い表示を最新だと読ませない**(カレンダー画面と同じ考え)
  sync.subscribe((status) => {
    const received = status && status.last_received_at;
    if (!received || drawnReceived === null || received === drawnReceived) return;
    if (document.querySelector(".modal")) return;       // 見ている途中は触らない
    drawnReceived = received;
    if (view) load(view.year, view.month);
  });

  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, .modal")) return;
    if (event.key === "PageUp") { event.preventDefault(); move(-1); }
    if (event.key === "PageDown") { event.preventDefault(); move(1); }
  });

  load();
}

/* ------------------------------------------------------------------
   読み込みと描画
   ------------------------------------------------------------------ */
async function load(year, month) {
  const query = (year && month) ? `?year=${year}&month=${month}` : "";
  try {
    render(await api.get(`/api/history${query}`));
  } catch (err) {
    if (err instanceof ApiError && err.status === 422) {
      await inform("その月は選べません", err.message);
      return;
    }
    toastError(err);
  }
}

function render(payload) {
  choices = payload.choices;
  const month = payload.month;
  const current = sync.current();
  drawnReceived = (current && current.last_received_at) || "";

  if (!month) {
    // 過去の登録が無い。**空のマス目は出さない**(何も無いのか、読めて
    // いないのかが分からなくなる)
    view = null;
    el.title.textContent = "履歴";
    el.empty.textContent = choices.message || "過去の月の登録がありません。";
    el.empty.hidden = false;
    el.grid.hidden = true;
    [el.prev, el.next, el.pick, el.print].forEach((b) => { b.disabled = true; });
    return;
  }

  view = month;
  el.empty.hidden = true;
  el.grid.hidden = false;
  el.title.textContent = `${month.title}(登録 ${month.count} 件)`;
  el.prev.disabled = !month.can_prev;
  el.next.disabled = !month.can_next;
  el.pick.disabled = false;
  el.print.disabled = false;

  renderHeaders(el.headers, month.weekdays);
  renderCells(el.grid, month.cells, viewDay);
  el.grid.setAttribute("aria-busy", "false");
}

function move(delta) {
  if (!view) return;
  const index = view.year * 12 + (view.month - 1) + delta;
  load(Math.floor(index / 12), (index % 12) + 1);
}

/* ------------------------------------------------------------------
   日付を押したら、その日の登録内容を出す(読むだけ)
   ------------------------------------------------------------------ */
async function viewDay(cell) {
  let day;
  try {
    day = await api.get(`/api/history/day/${cell.date}`);
  } catch (err) {
    toastError(err);
    return;
  }
  await modal({
    title: `${day.title} の登録内容`,
    tag: "閲覧のみ",
    wide: true,
    render(body) {
      const box = document.createElement("div");
      if (day.count) {
        box.className = "viewer";
        box.textContent = day.detail;
      } else {
        box.className = "viewer viewer--empty";
        box.textContent = "この日に登録データはありません。";
      }
      body.appendChild(box);
    },
    actions: [{ label: "閉じる", value: true, kind: "primary" }],
  });
}

/* ------------------------------------------------------------------
   年月を選ぶ

   年ごとに12か月のマスを並べる。**選べない月は押せない**(今月以降・
   登録より前)── どれを選べるかはサーバが返したとおり。
   ------------------------------------------------------------------ */
async function pickMonth() {
  if (!choices || !choices.years.length) return;
  const years = choices.years;
  let at = Math.max(0, years.findIndex((y) => view && y.year === view.year));

  const chosen = await modal({
    title: "見たい年月を選んでください",
    tag: "履歴",
    render(body, close) {
      const box = document.createElement("div");
      box.className = "mpick";

      const head = document.createElement("div");
      head.className = "mpick__head";
      const back = document.createElement("button");
      back.type = "button";
      back.className = "btn";
      back.textContent = "← 前の年";
      const label = document.createElement("strong");
      label.className = "mpick__year";
      const fwd = document.createElement("button");
      fwd.type = "button";
      fwd.className = "btn";
      fwd.textContent = "次の年 →";
      head.append(back, label, fwd);

      const grid = document.createElement("div");
      grid.className = "mpick__grid";
      box.append(head, grid);
      body.appendChild(box);

      function draw() {
        const year = years[at];
        label.textContent = `${year.year}年(登録 ${year.total} 件)`;
        back.disabled = at <= 0;
        fwd.disabled = at >= years.length - 1;
        grid.replaceChildren(...year.months.map((m) => {
          const tile = document.createElement("button");
          tile.type = "button";
          tile.className = "mpick__month";
          tile.disabled = !m.selectable;
          if (view && view.year === year.year && view.month === m.month) {
            tile.setAttribute("aria-current", "true");
          }
          const name = document.createElement("span");
          name.className = "mpick__name";
          name.textContent = m.label;
          const count = document.createElement("span");
          count.className = "mpick__count";
          // 選べない月は「—」。**件数0と区別する**(0件も記録)
          count.textContent = m.selectable ? `${m.count} 件` : "—";
          tile.append(name, count);
          tile.addEventListener("click", () => close({ year: year.year, month: m.month }));
          return tile;
        }));
      }
      back.addEventListener("click", () => { at -= 1; draw(); });
      fwd.addEventListener("click", () => { at += 1; draw(); });
      draw();
    },
    actions: [{ label: "取り消し", value: null }],
  });
  if (chosen) load(chosen.year, chosen.month);
}

/* ------------------------------------------------------------------
   印刷(カレンダー画面と同じ1枚)
   ------------------------------------------------------------------ */
function openPrint() {
  if (!view) return;
  // デスクトップ版は外枠が別の窓で開く(`desktop.js`)。ブラウザ版は別タブ
  openWindow(tokenUrl(`/print?year=${view.year}&month=${view.month}`),
             `${view.year}年${view.month}月 印刷`);
}
