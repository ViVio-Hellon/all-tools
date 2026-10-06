/*
  views/records.js — 記録を見る

  この画面がするのは2つだけ:

      見る    紙(印刷用HTML)を別窓で開く。**読むだけ**
      呼び出す 過去の直を日報入力に開き直す。**直せる状態になる**

  「ほかのPCの日報(控え)」(v4.12.0、管理者だけ)も同じ2つです。呼び出すほうは
  控えから手元へ入れてから開きます(`/api/records/backup/open`)。

  判断はどちらもサーバが持ちます ── 呼び出してよいか(管理者モードか)も、
  その直に何ページあるかも、ここでは決めません。
*/
import { api, tokenUrl } from "../api.js";
import { lineLabel } from "../line_label.js";
import { toast, toastError } from "../toast.js";

let loaded = null;

function key() {
  return {
    report_date: document.getElementById("p-date").value,
    line: document.getElementById("p-line").value,
    shift: document.getElementById("p-shift").value,
    page: Number(document.getElementById("p-page").value) || 1,
  };
}

function note(text, kind = "info") {
  const box = document.getElementById("note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

function recallNote(text, kind = "info") {
  const box = document.getElementById("recall-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

function paint(rows) {
  const body = document.getElementById("preview");
  if (!body) return;
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.className = "empty";
    td.textContent = "入力されている行がありません。";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const r of rows) {
    const tr = document.createElement("tr");
    for (const [value, cls] of [[r.row_no, ""], [r.lot, ""], [r.zai, ""],
                                [r.siz, ""], [r.mai, "num"], [r.wei, "num"]]) {
      const td = document.createElement("td");
      td.className = cls;
      td.textContent = value ?? "";
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
}

/** 紙の窓を開く。ヘッダを付けられない開き方なので、トークンはクエリに。 */
function openPaper(params) {
  const q = new URLSearchParams(params).toString();
  window.open(tokenUrl(`/report/nippou?${q}`), "_blank", "noopener");
}

/* この直にどのページがあるか。**ページ番号を当てずっぽうで打たせない。** */
function pagesNote(pages) {
  if (!pages || !pages.length) return "";
  return ` / この直にあるページ: ${pages.join("・")}`;
}

export function start() {
  // **画面へ来るたびに白紙から。** 前に開いていたときの「読込済み」を
  // 引きずると、別の鍵の下見を出したまま紙を開いてしまう
  loaded = null;

  document.getElementById("load")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/print/load", key());
      if (!body.found) {
        loaded = null;
        note(body.message, "warn");
        paint([]);
        document.getElementById("print").disabled = true;
        return;
      }
      loaded = key();
      note(`読み込みました: 担当者=${body.worker} / 明細${body.rows.length}行`
           + pagesNote(body.pages));
      paint(body.rows);
      document.getElementById("print").disabled = false;
    } catch (err) { toastError(err); }
  });

  document.getElementById("print")?.addEventListener("click", () => {
    if (!loaded) { toast("先に「中身を見る」を押してください", "warn"); return; }
    openPaper(loaded);
  });

  // **この直ぜんぶ。** 下見が済んでいなくても開ける ── 見るだけなら
  // 先に中身を確かめる必要はないし、開いたものがそのまま下見になる
  /*
    日付を指定して呼び出す。**ページまで指定できるのはここだけ**で、
    一覧に載らないほど古い直の2ページ目を直したいときの道です。
    通してよいかはサーバが決めます(`recall_refusal`)。
  */
  document.getElementById("recall-here")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/settings/recall", key());
      toast(body.message, "ok");
      location.href = body.next || "/";
    } catch (err) { note(err.message, "error"); }
  });

  /*
    この直を後から作る(後日作成・管理者)── **保存の無い直**を作る道。
    呼び出しは保存のある直しか開けないので、丸ごと打ち忘れた直はここから。
    まず確かめるだけ(`dry_run`)を送り、どのページを作るか・気をつけることを
    サーバの言葉のまま見せてから作ります。作れるかはサーバが決めます
    (`logic/backfill.py`: 管理者・終わった直だけ・空き枠だけ)。
  */
  document.getElementById("backfill-here")?.addEventListener("click", async () => {
    const target = { report_date: key().report_date, line: key().line, shift: key().shift };
    try {
      const dry = await api.post("/api/settings/backfill", { ...target, dry_run: true });
      const d = dry.decision;
      const caution = d.caution ? `\n\n※ ${d.caution}` : "";
      if (!confirm(`${d.message}${caution}\n\n保存すると「後日作成」の印が残ります。`
        + "日報入力に開きます。よろしいですか?")) return;
      const body = await api.post("/api/settings/backfill", {
        ...target, note: document.getElementById("backfill-reason")?.value || "",
      });
      toast(body.message, "ok");
      location.href = body.next || "/";
    } catch (err) { note(err.message, "error"); }
  });

  document.getElementById("print-all")?.addEventListener("click", () => {
    openPaper({ ...key(), page: "all" });
  });

  // 一覧・いまの直から1押しで紙を見る
  for (const btn of document.querySelectorAll("[data-open-all]")) {
    btn.addEventListener("click", () => {
      openPaper({ report_date: btn.dataset.date, line: btn.dataset.line,
                  shift: btn.dataset.shift, page: "all" });
    });
  }

  /*
    呼び出す ── **紙を見るのとは違う。** その直が日報入力に開いて、
    直せる状態になります。だから行き先(日報入力)へ連れて行きます:
    押したのに画面が変わらないと、開いたつもりで今日のぶんを打ちます。
  */
  for (const btn of document.querySelectorAll("[data-recall]")) {
    btn.addEventListener("click", async () => {
      // ページが複数ある直は、隣の選択欄で選ばせる。1ページだけなら欄が無いので
      // ボタンが持っているページ(=その1ページ)をそのまま使う
      const picker = btn.closest("td")?.querySelector("[data-recall-page]");
      const page = Number(picker?.value || btn.dataset.page) || 1;
      try {
        const body = await api.post("/api/settings/recall", {
          report_date: btn.dataset.date, line: btn.dataset.line,
          shift: btn.dataset.shift, page,
        });
        toast(body.message, "ok");
        location.href = body.next || "/";
      } catch (err) { recallNote(err.message, "error"); }
    });
  }

  wireBackup();

  // 呼び出しをやめて、いまの直に戻る(**戻る前に保存されます**)
  document.getElementById("back-now")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/settings/back", {});
      toast(body.message, "ok");
      location.href = body.next || "/";
    } catch (err) { recallNote(err.message, "error"); }
  });
}

/* ================================================================
   ほかのPCの日報(控え LocalBackup)── v4.12.0、管理者だけに出る札
   ================================================================ */
function backupNote(text, kind = "info") {
  const box = document.getElementById("backup-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

function cell(text, cls = "") {
  const td = document.createElement("td");
  if (cls) td.className = cls;
  td.textContent = text ?? "";
  return td;
}

function backupRow(s) {
  const tr = document.createElement("tr");
  tr.append(cell(s.report_date), cell(lineLabel(s.line)), cell(s.shift),
            cell(`${s.pages.join("・")}ページ`), cell((s.workers || []).join(" ")),
            cell(s.saved_at || "―"));
  const sent = cell("");
  if (s.synced) sent.textContent = "済";
  else { const b = document.createElement("b"); b.textContent = "未"; sent.append(b); }
  tr.append(sent);

  const actions = document.createElement("td");
  const look = document.createElement("button");
  look.type = "button";
  look.className = "btn btn--sm";
  look.textContent = "紙を見る";
  look.addEventListener("click", () => openPaper({
    report_date: s.report_date, line: s.line, shift: s.shift, page: "all" }));
  actions.append(look, " ");
  let picker = null;
  if (s.pages.length > 1) {
    picker = document.createElement("select");
    picker.className = "recall-page";
    picker.setAttribute("aria-label", `${s.report_date} ${s.shift} のページ`);
    for (const n of s.pages) picker.append(new Option(`ページ${n}`, String(n)));
    actions.append(picker, " ");
  }
  const open = document.createElement("button");
  open.type = "button";
  open.className = "btn btn--sm";
  open.textContent = "呼び出して直す";
  open.dataset.hint = s.in_local
    ? "このPCにもある直です。このPCのほうが新しいページはそのまま、控えのほうが新しいページは控えで開きます"
    : "控えからこのPCへ入れて、日報入力に開きます";
  open.addEventListener("click", async () => {
    const page = Number(picker?.value || s.pages[0]) || 1;
    try {
      const body = await api.post("/api/records/backup/open", {
        report_date: s.report_date, line: s.line, shift: s.shift, page });
      toast(body.message, "ok");
      location.href = body.next || "/";
    } catch (err) { backupNote(err.message, "error"); }
  });
  actions.append(open);
  tr.append(actions);
  return tr;
}

async function loadBackup() {
  const select = document.getElementById("backup-line");
  const body = document.getElementById("backup-rows");
  if (!select || !body) return;
  try {
    const view = await api.get(`/api/records/backup?line=${encodeURIComponent(select.value)}`);
    body.replaceChildren();
    if (!view.shifts.length) {
      const tr = document.createElement("tr");
      const td = cell(`${lineLabel(view.line)} の控えに直がありません`, "empty");
      td.colSpan = 8;
      tr.append(td);
      body.append(tr);
    }
    for (const s of view.shifts) body.append(backupRow(s));
    backupNote("");
  } catch (err) { backupNote(err.message, "error"); }
}

function wireBackup() {
  if (!document.getElementById("backup-card")) return;
  document.getElementById("backup-line")?.addEventListener("change", loadBackup);
  document.getElementById("backup-reload")?.addEventListener("click", loadBackup);
  loadBackup();
}
