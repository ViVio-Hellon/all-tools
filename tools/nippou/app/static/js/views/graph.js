/*
  views/graph.js — 集計・グラフ(ダッシュボード)

  **主表示は「並べて見る」ほう。** VBA は `graphF` のタブで1枚ずつ
  切り替えていたが、それだと隣が見えない ── 「重量は出ているのに
  稼働率が低い」のような、並べて初めて分かることが落ちる。
  切り替えは「1つずつ大きく見る」に残してある。

  **何を出すか・どう集計するかはサーバが決める**
  (`presenters/dashboard.py`)。ここは絵を器へ入れる係。数字と表は
  サーバが描いたものがすでに入っているので触らない ── JSが転んでも
  読めるようにしておく。

  【`start()` の中で全部やる理由】
  この画面へ2度目に来たとき、モジュールは読み直されない(ES モジュールは
  一度きり)。**要素は毎回引き直す**必要があるので、配線はここに閉じる。
*/

import { api } from "../api.js";
import { stampAsOf } from "../asof.js";
import { toast, toastError } from "../toast.js";
import { paintTile } from "../chart.js";
// グラフと集計の見た目(ネオン / 元のまま)。**上書きの層の on/off** だけ
import * as skin from "../skin.js";

/** いま画面に出ているぶん。切り替えのときに読み直す */
let current = null;

function tileOf(key) {
  return (current?.tiles || []).find((t) => t.key === key) || null;
}

/** タイルの絵を入れる。数字と表はサーバが描いたものをそのまま残す。 */
function paintTiles(dashboard) {
  current = dashboard;
  for (const tile of dashboard.tiles || []) {
    if (tile.kind === "number" || tile.kind === "table") continue;
    paintTile(document.querySelector(`[data-tile-body="${tile.key}"]`), tile);
  }
}

/** 下の「1つずつ大きく見る」。選ばれた1枚だけを大きく描く。 */
function paintFocus(key) {
  const host = document.getElementById("focus-body");
  const note = document.getElementById("focus-note");
  const tile = tileOf(key);
  if (!host || !tile) return;
  paintTile(host, tile);
  if (note) note.textContent = tile.note ? `${tile.note} / ${tile.unit}` : tile.unit;
}

/**
 * 期間を変えたあと、画面ごと描き直す。
 *
 * 数字と表もサーバが作り直すので、**画面を取り直して差し替える** ──
 * JS側で表を組み立てると、同じ表の作り方が Jinja と2か所に分かれる。
 */
/**
 * タイルの器を1枚作る。**Jinja が最初に描くものと同じ形**
 * (`graph.html` の `.tile`)。
 *
 * 器まで作るのは、**並ぶタイルが入れ替わるから**です ── その日と期間で
 * 出す数字が違います(本日の合計重量 / 期間の合計重量)。前は器を
 * Jinja が描いたものだけに頼っていたので、期間へ切り替えても
 * **「本日の…」のタイルが残ったまま**で、期間のタイルはどこにも
 * 出ませんでした(器が無いので入れる先が無い)。
 */
function shellFor(tile) {
  const box = document.createElement("div");
  box.className = "tile"
    + (["bar", "line", "donut"].includes(tile.kind) ? " tile--wide" : "")
    + (["combo", "table"].includes(tile.kind) ? " tile--full" : "");
  box.dataset.tile = tile.key;
  if (tile.tone) box.dataset.tone = tile.tone;

  const head = document.createElement("div");
  head.className = "tile__head";
  const title = document.createElement("h2");
  title.className = "tile__title";
  title.textContent = tile.title;
  const note = document.createElement("span");
  note.className = "tile__note";
  note.textContent = tile.note || "";
  head.append(title, note);

  const body = document.createElement("div");
  body.className = "tile__body";
  body.dataset.tileBody = tile.key;

  const foot = document.createElement("div");
  foot.className = "tile__foot";
  if (tile.link) {
    const link = document.createElement("a");
    link.href = tile.link;
    link.textContent = tile.link_text || "詳しく見る";
    foot.append(link);
  } else if (["bar", "line", "donut"].includes(tile.kind)) {
    const go = document.createElement("button");
    go.className = "btn btn--sm";
    go.type = "button";
    go.dataset.focus = tile.key;
    go.textContent = "大きく見る";
    foot.append(go);
  }
  box.append(head, body, foot);
  return box;
}

/** 「1つずつ大きく見る」の選べるもの。**並ぶタイルと一緒に入れ替える。** */
function refillChoices(dashboard) {
  const pick = document.getElementById("focus-pick");
  if (!pick) return;
  const before = pick.value;
  pick.replaceChildren(...(dashboard.choices || []).map((c) => {
    const option = document.createElement("option");
    option.value = c.key;
    option.textContent = c.title;
    return option;
  }));
  // 前に見ていたものが残っていれば、そのまま。無ければ先頭へ
  pick.value = (dashboard.choices || []).some((c) => c.key === before)
    ? before : (dashboard.choices?.[0]?.key || "");
}

function replaceTiles(dashboard) {
  // **器ごと並べ直す。** その日と期間で並ぶタイルが入れ替わるので、
  // 「あるものだけ塗り替える」では古いタイルが残ります
  const host = document.getElementById("tiles");
  if (host) {
    host.replaceChildren(
      ...(dashboard.tiles || []).map((tile) => shellFor(tile)));
  }
  for (const tile of dashboard.tiles || []) {
    // **見出しの添え書きも塗り直す。** ここには「いつのぶんか」
    // (2026-08-01 〜 2026-08-31 / 本日)が出ています。数字だけ入れ替えると
    // **新しい数字に古い期間の札が付いたまま**になり、見ている人は
    // 期間を変えたことに気づけません
    const head = document.querySelector(
      `[data-tile="${tile.key}"] .tile__note`);
    if (head) head.textContent = tile.note || "";

    const body = document.querySelector(`[data-tile-body="${tile.key}"]`);
    if (!body) continue;
    if (tile.kind === "number") {
      body.replaceChildren();
      const value = document.createElement("div");
      value.className = "tile__value";
      value.textContent = tile.value;
      const unit = document.createElement("span");
      unit.className = "tile__unit";
      unit.textContent = tile.unit;
      value.append(unit);
      body.append(value);
      // **途中式も作り直す。** 忘れると、期間を変えた瞬間に式だけ消える
      // ── しかも数字は新しいので、消えたことに気づけません
      const calc = calcBox(tile);
      if (calc) body.append(calc);
    } else if (tile.kind === "table") {
      body.replaceChildren(table(tile));
    }
  }
  refillChoices(dashboard);
  paintTiles(dashboard);
}

/**
 * 計算の途中式。**式を組み立てるのはサーバ**(`logic/formula.py`)で、
 * ここは受け取った3行を並べるだけです ── ここで組み立てると、画面と
 * CSVで違う式が出ます。
 *
 * 途中式の無いタイル(足すだけの合計)には `null` を返します。
 */
function calcBox(tile) {
  if (!tile.steps?.length) return null;
  const box = document.createElement("details");
  box.className = "calc";
  const open = document.createElement("summary");
  open.className = "calc__open";
  open.textContent = "計算内容を見る";
  const list = document.createElement("ol");
  list.className = "calc__list";
  for (const step of tile.steps) {
    const item = document.createElement("li");
    item.className = "calc__step";
    const parts = [
      ["b", "calc__name", step.name],
      ["code", "calc__formula", step.formula],
      ["code", "calc__sub", `= ${step.substituted}`],
      ["code", "calc__result", `= ${step.result}`],
    ];
    if (step.note) parts.push(["span", "calc__note", step.note]);
    for (const [tag, cls, text] of parts) {
      const node = document.createElement(tag);
      node.className = cls;
      node.textContent = text;
      item.append(node);
    }
    list.append(item);
  }
  box.append(open, list);
  return box;
}

/** 表1つ。期間を変えたときだけ使う(初回はサーバが描いている)。 */
function table(tile) {
  if (!tile.rows?.length) {
    const note = document.createElement("p");
    note.className = "lead tile__empty";
    note.textContent = tile.empty;
    return note;
  }
  const box = document.createElement("div");
  box.className = "scroll-x";
  const el = document.createElement("table");
  el.className = "list";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  tile.columns.forEach((name, i) => {
    const th = document.createElement("th");
    if (i) th.className = "num";
    th.textContent = name;
    headRow.append(th);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  for (const row of tile.rows) {
    const tr = document.createElement("tr");
    row.forEach((cell, i) => {
      const td = document.createElement("td");
      if (i) td.className = "num";
      td.textContent = cell;
      tr.append(td);
    });
    body.append(tr);
  }
  el.append(head, body);
  box.append(el);
  return box;
}

/** 書き出した結果を画面に残す。**トーストは消えるが、ここは残る。** */
function showNote(text, kind = "info") {
  const box = document.getElementById("csv-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

export function start() {
  // 見た目の切り替え。**画面を移るたびに繋ぎ直す**(つまみは差し替えで
  // 作り直されるので)。当たるのはこの画面と集計管理だけです
  skin.wire(document.getElementById("neon-skin"));

  const seed = document.getElementById("dashboard-data");
  if (seed) {
    try { paintTiles(JSON.parse(seed.textContent)); } catch { /* 題は出ている */ }
  }

  const pick = document.getElementById("focus-pick");
  if (pick) {
    // 最初は先頭の絵。数字や表を大きくしても仕方がない
    const first = (current?.tiles || []).find(
      (t) => t.kind === "bar" || t.kind === "line" || t.kind === "donut");
    if (first) pick.value = first.key;
    paintFocus(pick.value);
    pick.addEventListener("change", () => paintFocus(pick.value));
  }

  // タイルの「大きく見る」。下の切り替えを合わせてから連れて行く。
  //
  // **1枚ずつではなく、置き場所に付けます**(委譲)── タイルは期間へ
  // 切り替えるたびに作り直されるので、ボタンに直接付けると2回目から
  // 効かなくなります
  document.getElementById("tiles")?.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-focus]");
    if (!button) return;
    const key = button.dataset.focus;
    if (pick) pick.value = key;
    paintFocus(key);
    document.getElementById("focus-body")
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  document.getElementById("reload")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/graph/dashboard", {
        start: document.getElementById("start").value,
        end: document.getElementById("end").value,
        // 目標線を出すかどうか(VBA `Targetline`)。**判断は人**
        with_target: document.getElementById("with-target")?.checked !== false,
        // その日を見るのか、期間を見るのか。**数え直すのはサーバ**
        mode: document.getElementById("mode")?.value || "day",
      });
      replaceTiles(body);
      if (pick) paintFocus(pick.value);
      // **いつ引いた数字かも一緒に更新する。** 忘れると、絵だけ新しく
      // なって時刻が前のまま残る ── いちばん悪い形の嘘になります
      stampAsOf(body.generated_at);
      toast("表示を更新しました", "ok");
    } catch (err) { toastError(err); }
  });

  // 目標線のチェックは、押した時点で引き直す ── 「表示」を
  // もう一度押させると、切ったつもりで切れていない時間ができる
  document.getElementById("with-target")?.addEventListener("change", () => {
    document.getElementById("reload")?.click();
  });

  // 見せ方(その日 / 期間)も、選んだ時点で引き直す ── 選んだのに
  // 画面が前のままだと、切り替わっていないように見えます
  document.getElementById("mode")?.addEventListener("change", () => {
    document.getElementById("reload")?.click();
  });

  // **画面に出している期間ぶんを出す。** 期間を決めて見ているのに
  // 今日のぶんしか出ないと、過去のぶん(取り込んだ日など)が出せない
  document.getElementById("csv")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/graph/csv", {
        start: document.getElementById("start")?.value || "",
        end: document.getElementById("end")?.value || "",
      });
      showNote(body.message, body.day_count ? "ok" : "warn");
      toast(body.day_count
        ? `集計CSVを ${body.day_count}日ぶん書き出しました`
        : "出せるものがありませんでした", body.day_count ? "ok" : "warn");
    } catch (err) {
      showNote(err.message, "error");
      toastError(err);
    }
  });
}
