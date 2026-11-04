/*
  views/removal.js — 設定・管理者の「直を消す」と「テストモード」

  **探すもの・消してよいか・共有から消すかを決めるのはサーバ**(`services/shift_removal`・
  `nippou/test_mode`)。ここは並べて、選んだものを送るだけです。

  確かめは**画面の中の窓**(`#removal-confirm` / `#removal-ask`)。`confirm()` はデスクトップ版の
  窓では出ないので使いません。

      探す → 選ぶ → 「選んだページを手元から消す」→ 窓で確かめる → 手元から消す
           → 「共有の日報データからも消しますか？」→ はい: 共有からも / いいえ: 手元だけ
*/
import { api } from "../api.js";
import { enterTestMode as enter, leaveTestMode } from "../test_band.js";
import { toastError } from "../toast.js";

let listed = [];          // いま出している一覧(サーバが返したまま)
let lastIds = [];         // 直前に手元から消した記録の番号(共有からも消すかの問い)

const el = (id) => document.getElementById(id);

function note(text, kind = "info") {
  const box = el("removal-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text || "";
  box.className = `msg msg--${kind}`;
}

function cell(tr, text, title = "") {
  const td = document.createElement("td");
  td.textContent = text;
  if (title) td.title = title;
  tr.append(td);
  return td;
}

function fillChoices(select, items) {
  if (!select) return;
  const keep = select.value;
  select.replaceChildren(new Option("すべて", ""));
  for (const it of items || []) select.append(new Option(it.label, it.code));
  if ([...select.options].some((o) => o.value === keep)) select.value = keep;
}

function picked() {
  return [...document.querySelectorAll("#removal-rows input[data-removal]:checked")]
    .map((box) => listed[Number(box.dataset.removal)]).filter(Boolean);
}

function countPicked() {
  const n = picked().length;
  const out = el("removal-picked");
  if (out) out.textContent = n ? `${n}ページを選んでいます` : "";
}

function paint(body) {
  listed = body.pages || [];
  const head = el("removal-head");
  if (head) head.textContent = body.headline || "";
  fillChoices(el("removal-line"), body.choices?.lines);
  fillChoices(el("removal-shift"), body.choices?.shifts);
  const rows = el("removal-rows");
  const box = el("removal-box");
  if (rows && box) {
    rows.replaceChildren();
    box.hidden = !listed.length;
    listed.forEach((p, i) => {
      const tr = document.createElement("tr");
      tr.dataset.key = p.key_text;
      if (p.test) tr.classList.add("is-test");
      const pick = document.createElement("td");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.dataset.removal = String(i);
      check.setAttribute("aria-label", `${p.key_text} を選ぶ`);
      check.addEventListener("change", countPicked);
      pick.append(check);
      tr.append(pick);
      cell(tr, p.report_date);
      cell(tr, p.line_label || p.line);
      cell(tr, p.shift);
      cell(tr, `第${p.page}ページ`);
      cell(tr, p.worker || "(空)");
      cell(tr, `${p.rows}行`);
      const marks = [p.test ? "テスト" : "", p.empty ? "中身なし" : ""].filter(Boolean);
      const mark = cell(tr, "");
      for (const m of marks) {
        const badge = document.createElement("span");
        badge.className = `badge ${m === "テスト" ? "badge--todo" : ""}`;
        badge.textContent = m;
        mark.append(badge, " ");
      }
      cell(tr, p.unsent ? (p.sent ? "送ったあとに直した(未送信)" : "未送信")
                        : (p.sent ? "送った" : "—"));
      cell(tr, p.saved_at || "");
      rows.append(tr);
    });
  }
  const all = el("removal-all");
  if (all) all.checked = false;
  const pickTest = el("removal-pick-test");
  if (pickTest) pickTest.hidden = !listed.some((p) => p.test);
  countPicked();
  paintOpen(body.open || []);
}

function paintOpen(items) {
  const wrap = el("removal-open");
  const rows = el("removal-open-rows");
  if (!wrap || !rows) return;
  wrap.hidden = !items.length;
  rows.replaceChildren();
  for (const it of items) {
    const tr = document.createElement("tr");
    const pick = document.createElement("td");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.dataset.openId = it.id;
    check.setAttribute("aria-label", `${it.key_text} を選ぶ`);
    pick.append(check);
    tr.append(pick);
    cell(tr, it.key_text + (it.test ? "(テスト)" : ""));
    cell(tr, it.removed_at);
    cell(tr, it.state);
    cell(tr, it.detail || "");
    rows.append(tr);
  }
}

function query() {
  const params = new URLSearchParams({
    kind: el("removal-kind")?.value || "test",
    day: el("removal-day")?.value || "",
    line: el("removal-line")?.value || "",
    shift: el("removal-shift")?.value || "",
  });
  return `/api/settings/removal?${params}`;
}

async function find(quiet = false) {
  try {
    const body = await api.get(query());
    paint(body);
    if (!quiet) note("", "info");
    return body;
  } catch (err) {
    note(err.message, "error");
    return null;
  }
}

/* ---- 窓 ---------------------------------------------------------- */
function openModal(id) {
  const box = el(id);
  if (!box) return;
  box.hidden = false;
  box.querySelector(".modal__foot .btn--danger")?.focus();
}

function closeModal(id) {
  const box = el(id);
  if (box) box.hidden = true;
}

function askDelete(pages) {
  const text = el("removal-confirm-text");
  const list = el("removal-confirm-list");
  if (text) {
    const tests = pages.filter((p) => p.test).length;
    const sent = pages.filter((p) => p.sent).length;
    text.textContent = `選んだ ${pages.length}ページを、この PC の手元から消します`
      + (tests ? `(うちテストデータ ${tests}ページ)` : "")
      + (sent ? `。${sent}ページは共有へ送ってあります(共有から消すかは、このあと聞きます)` : "")
      + "。消す前の控えをこの PC に残します。";
  }
  if (list) {
    list.replaceChildren();
    for (const p of pages.slice(0, 12)) {
      const li = document.createElement("li");
      li.textContent = `${p.key_text}(${p.rows}行)`;
      list.append(li);
    }
    if (pages.length > 12) {
      const li = document.createElement("li");
      li.textContent = `ほか ${pages.length - 12}ページ`;
      list.append(li);
    }
  }
  openModal("removal-confirm");
}

async function doDelete() {
  closeModal("removal-confirm");
  const pages = picked();
  if (!pages.length) return;
  try {
    const body = await api.post("/api/settings/removal/delete", {
      items: pages.map((p) => ({
        report_date: p.report_date, line: p.line, shift: p.shift, page: p.page,
        stamp: p.stamp, reason: el("removal-kind")?.value || "all",
      })),
    });
    note(body.message, body.skipped?.length ? "warn" : "ok");
    lastIds = body.ids || [];
    await find(true);
    if (body.ask_shared && lastIds.length) {
      const text = el("removal-ask-text");
      if (text) text.textContent = `手元から ${lastIds.length}ページを消しました。`;
      openModal("removal-ask");
    }
  } catch (err) {
    // 断られた(打たれていた・もう無い・控えを残せなかった)。**一覧を出し直す**
    note(err.message, "warn");
    await find(true);
  }
}

async function answerShared(remove, ids) {
  if (!ids.length) return;
  try {
    const body = await api.post("/api/settings/removal/shared", { ids, remove });
    note(body.message, "ok");
    paintOpen(body.open || []);
  } catch (err) {
    // 共有に届かなかった(503)。手元は消したまま、共有はまだ、として残っている
    note(err.message, "warn");
    paintOpen(err.body?.open || []);
  }
}

function openPicked() {
  return [...document.querySelectorAll("#removal-open-rows input[data-open-id]:checked")]
    .map((box) => box.dataset.openId);
}

/* ---- テストモード ------------------------------------------------ */
function testNote(text, kind) {
  const box = el("test-mode-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text || "";
  box.className = `msg msg--${kind}`;
}

async function enterTestMode() {
  const pw = el("test-mode-pw");
  try {
    const password = pw?.value || "";
    if (pw) pw.value = "";
    // 帯(外枠)を描き直して、この面は差し替えで取り直す(`test_band.js`)
    await enter(password);
  } catch (err) {
    if (pw) pw.value = "";
    testNote(err.message, "error");
  }
}

export function start() {
  listed = [];
  lastIds = [];
  el("removal-find")?.addEventListener("click", () => find());
  el("removal-kind")?.addEventListener("change", () => find());
  el("removal-all")?.addEventListener("change", (event) => {
    for (const box of document.querySelectorAll("#removal-rows input[data-removal]")) {
      box.checked = event.target.checked;
    }
    countPicked();
  });
  el("removal-pick-test")?.addEventListener("click", () => {
    for (const box of document.querySelectorAll("#removal-rows input[data-removal]")) {
      box.checked = Boolean(listed[Number(box.dataset.removal)]?.test);
    }
    countPicked();
  });
  el("removal-delete")?.addEventListener("click", () => {
    const pages = picked();
    if (!pages.length) {
      note("消すページを選んでください", "warn");
      return;
    }
    askDelete(pages);
  });
  el("removal-confirm-no")?.addEventListener("click", () => closeModal("removal-confirm"));
  el("removal-confirm-yes")?.addEventListener("click", doDelete);
  el("removal-ask-yes")?.addEventListener("click", async () => {
    closeModal("removal-ask");
    await answerShared(true, lastIds);
  });
  el("removal-ask-no")?.addEventListener("click", async () => {
    closeModal("removal-ask");
    await answerShared(false, lastIds);
  });
  el("removal-open-all")?.addEventListener("change", (event) => {
    for (const box of document.querySelectorAll("#removal-open-rows input[data-open-id]")) {
      box.checked = event.target.checked;
    }
  });
  el("removal-open-shared")?.addEventListener("click", () => answerShared(true, openPicked()));
  el("removal-open-local")?.addEventListener("click", () => answerShared(false, openPicked()));

  el("test-mode-enter")?.addEventListener("click", enterTestMode);
  el("test-mode-pw")?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    enterTestMode();
  });
  el("test-mode-leave")?.addEventListener("click", async () => {
    try {
      await leaveTestMode();
    } catch (err) { toastError(err); }
  });
}
