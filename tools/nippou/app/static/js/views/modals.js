/*
  views/modals.js — 作業者選択 / 全停入力

  tkinter版では別ウィンドウ(`ui/staff_window.py` / `ui/formstop_window.py`)
  だったもの。どちらも「一覧から選ぶ → いまの入力へ返す」なので、
  画面を移らずにモーダルで開く。

  **一覧の中身も、選んだ結果の文字列も、サーバが作る。**
  ここは開く・閉じる・選択状態を持つだけ。
*/

import { api } from "../api.js";
import { toast, toastError } from "../toast.js";
import { pageSignal } from "../nav.js";

/* ---------------------------------------------------------------- */
/* モーダルの開閉                                                     */
/* ---------------------------------------------------------------- */
let lastFocus = null;

export function open(id) {
  const box = document.getElementById(id);
  if (!box) return;
  lastFocus = document.activeElement;
  box.hidden = false;
  box.querySelector("button, input, select")?.focus();
}

export function close(id) {
  const box = document.getElementById(id);
  if (!box) return;
  box.hidden = true;
  // 開く前に居た場所へ戻す。戻さないと、閉じた瞬間に焦点が本文の
  // 先頭へ飛んで、続きを入力できなくなる
  lastFocus?.focus?.();
}

function note(id, text, kind = "warn") {
  const box = document.getElementById(id);
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

/* ---------------------------------------------------------------- */
/* 作業者選択                                                         */
/* ---------------------------------------------------------------- */
let staffLoaded = false;

function paintTeams(teams) {
  const host = document.getElementById("staff-teams");
  if (!host) return;
  host.replaceChildren();
  for (const { team, names } of teams) {
    if (!names.length) continue;
    const box = document.createElement("div");
    // 幅と並びは CSS(`.staff-teams` ── 全部の班を横1列に。v4.16.0)
    box.className = "card staff-team";
    // **班の見出しは押しボタン**(v4.7.0)。押すと班の全員にチェック、全員に
    // 付いていれば全員外す ── 班で入る直がほとんどなので、1人ずつ押さない
    const head = document.createElement("button");
    head.type = "button";
    head.className = "staff-team__head";
    head.dataset.staffTeam = team;
    head.dataset.hint = `押すと ${team}班の全員にチェックが付きます(もう一度押すと全員外れます)`;
    head.textContent = `${team}班`;
    const count = document.createElement("span");
    count.className = "staff-team__all";
    count.textContent = "全員";
    head.appendChild(count);
    box.appendChild(head);
    for (const name of names) {
      const label = document.createElement("label");
      label.className = "check";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = name;
      input.dataset.staff = "1";
      input.dataset.staffOf = team;
      label.append(input, document.createTextNode(name));
      box.appendChild(label);
    }
    host.appendChild(box);
  }
  if (!host.children.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "名簿がありません。担当者欄へ直接入力してください。";
    host.appendChild(empty);
  }
}

async function loadStaff() {
  if (staffLoaded) return;
  try {
    const body = await api.get("/api/staff/members");
    paintTeams(body.teams || []);
    note("staff-note", body.message || "");
    staffLoaded = true;
  } catch (err) { toastError(err); }
}

/** 窓に並んでいる名前(名簿そのもの)。 */
function roster() {
  return [...document.querySelectorAll("[data-staff]")].map((el) => el.value);
}

function workerField() {
  return document.querySelector('[data-header="worker"]');
}

/**
 * **いま担当者欄に居る人に、最初からチェックを付ける**(v4.7.0)。
 *
 * 開くたびに全部外れていると、1人足すだけでも全員を選び直すことになる。
 * 誰に付けるかはサーバが決める(`logic/staff.preselect`)。
 */
async function preselectStaff() {
  const boxes = [...document.querySelectorAll("[data-staff]")];
  if (!boxes.length) return;
  try {
    const body = await api.post("/api/staff/preselect", {
      current: workerField()?.value || "", roster: roster(),
    });
    const checked = new Set(body.checked || []);
    for (const el of boxes) el.checked = checked.has(el.value);
  } catch (err) { toastError(err); }
}

/** 班の見出し ── 全員に付いていれば全員外す、そうでなければ全員に付ける。 */
function toggleTeam(team) {
  const boxes = [...document.querySelectorAll("[data-staff]")]
    .filter((el) => el.dataset.staffOf === team);
  const all = boxes.length && boxes.every((el) => el.checked);
  for (const el of boxes) el.checked = !all;
}

/* ---------------------------------------------------------------- */
/* 全停入力                                                           */
/* ---------------------------------------------------------------- */
let stopLoaded = false;
let picked = "";        // 画面に出す名前
let pickedCode = "";    // **欄に入るのはこちら**(内訳番号)

function paintCategories(categories) {
  const host = document.getElementById("formstop-categories");
  if (!host) return;
  host.replaceChildren();
  for (const { category, reasons } of categories) {
    const box = document.createElement("div");
    box.className = "card";
    box.style.minWidth = "180px";
    const head = document.createElement("b");
    head.textContent = category;
    box.appendChild(head);
    for (const reason of reasons) {
      const label = document.createElement("label");
      label.className = "check";
      label.style.display = "flex";
      const input = document.createElement("input");
      input.type = "radio";
      // **1つだけ選ばせる。** 同じ name にすると、カテゴリをまたいでも
      // 排他になる(tkinter版も1件だけ選ぶ作りだった)
      input.name = "formstop-reason";
      input.value = reason.code || reason.label;
      input.addEventListener("change", () => {
        picked = reason.label;
        // **記号を捨てない。** 作業停止①の欄が持つのは内訳番号で、
        // 名前を入れると保存前チェックが断り、そのページは保存できなく
        // なります(消した形も同じ断りに当たるので、やり直しもできない)
        pickedCode = reason.code || "";
        const out = document.getElementById("formstop-selected");
        if (out) {
          out.textContent = `${category} / ${reason.label}`
                          + (pickedCode ? `（記号 ${pickedCode}）` : "");
        }
      });
      label.append(input, document.createTextNode(reason.label));
      box.appendChild(label);
    }
    host.appendChild(box);
  }
}

async function loadReasons() {
  if (stopLoaded) return;
  try {
    const body = await api.get("/api/formstop/reasons");
    paintCategories(body.categories || []);
    note("formstop-note", body.message || "");
    stopLoaded = true;
  } catch (err) { toastError(err); }
}

/* ---------------------------------------------------------------- */
/* 配線                                                               */
/*                                                                    */
/* **画面へ来るたびに繋ぎ直す。** ES モジュールは一度しか読まれないので、 */
/* 差し替えで作り直された要素には、前回付けた listener が残っていない。  */
/* ---------------------------------------------------------------- */
export function start() {
  // 一覧は画面ごとに引き直す。前の画面で引いたものを使い回すと、
  // 名簿を直しても画面を移るまで反映されない
  staffLoaded = false;
  stopLoaded = false;
  picked = "";

  for (const btn of document.querySelectorAll("[data-close]")) {
    btn.addEventListener("click", () => close(btn.dataset.close));
  }
  // 背景を押したら閉じる
  for (const box of document.querySelectorAll(".modal")) {
    box.addEventListener("click", (event) => {
      if (event.target === box) close(box.id);
    });
  }
  // Esc でも閉じる。**`document` に付くので、画面を出るときに切る** ──
  // 切らないと画面を移るたびに増えて、Esc 1回で何度も閉じようとする
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    for (const box of document.querySelectorAll(".modal:not([hidden])")) close(box.id);
  }, { signal: pageSignal() });

  // **入口は2つ、開くのは同じ窓。** 直の始まりの帯からも押せます
  // (「まず作業者を選んでください」の大きいボタン)
  for (const id of ["open-staff", "start-pick-staff"]) {
    document.getElementById(id)?.addEventListener("click", async () => {
      open("staff-modal");
      await loadStaff();
      await preselectStaff();
    });
  }

  // 班の見出し(全員に付ける/外す)。**窓に1つだけ** ── 見出しは名簿を
  // 読むたびに作り直すので、要素ごとに付けると付け直しが要る
  document.getElementById("staff-teams")?.addEventListener("click", (event) => {
    const head = event.target.closest?.("[data-staff-team]");
    if (head) toggleTeam(head.dataset.staffTeam);
  });

  document.getElementById("staff-apply")?.addEventListener("click", async () => {
    const names = [...document.querySelectorAll("[data-staff]:checked")]
      .map((el) => el.value);
    try {
      // 名簿に無い字(手で打った名前・書き添え)は**消さずに残す**
      // (`logic/staff.merge_worker`)── そのために今の欄と名簿を添える
      const body = await api.post("/api/staff/apply", {
        names, current: workerField()?.value || "", roster: roster(),
      });
      const field = workerField();
      if (field) {
        field.value = body.worker;
        // **書き込んだだけでは blur が起きない。** 合図を出さないと、
        // 次に何か打つまでサーバは担当者が空のままだと思っている
        // (手順の帯が「作業者」で止まったままになる)
        field.dispatchEvent(new Event("change", { bubbles: true }));
      }
      toast(body.message, "ok");
      close("staff-modal");
    } catch (err) { toastError(err); }
  });

  document.getElementById("open-formstop")?.addEventListener("click", async () => {
    open("formstop-modal");
    await loadReasons();
  });

  document.getElementById("formstop-run")?.addEventListener("click", async () => {
    if (!picked) { toast("停止理由を選んでください", "warn"); return; }
    // **取り返しがつかない操作なので確かめる。** 新しいページとして保存される
    if (!confirm(`「${picked}」で全停入力します。よろしいですか?`)) return;
    try {
      const body = await api.post("/api/formstop/execute", {
        reason: picked, code: pickedCode,
        // **作業者を一緒に送る。** 全停は押した時点で1ページを書くので、
        // 誰の直かが決まっていないまま紙ができないように、サーバが断ります
        // (`no_worker`)。打ちかけの値を送るのは、保存前の欄がまだ
        // サーバに渡っていないことがあるためです
        worker: document.querySelector('[data-header="worker"]')?.value || "",
      });
      toast(body.message, "ok");
      close("formstop-modal");
      location.href = body.next || "/";
    } catch (err) {
      // **断られた先を出してやる。** 「すでに全停入力があります(第1
      // ページ)」で止まったとき、そのページへ行く道が画面になく、
      // 「消しても直せません」で手詰まりになっていました
      if (err.status === 422 && err.body?.error?.code === "already_all_stop"
          && err.body.page) {
        close("formstop-modal");
        showAllStopStuck(err.message, Number(err.body.page));
        return;
      }
      toastError(err);
    }
  });
}

/**
 * 全停が二重になったときの逃げ道 ── **そのページへ行くボタンを出す。**
 *
 * 文言はサーバのもの(`logic/pages.all_stop_refusal`)。ここは、そこに
 * 書いてある「第◯ページを開く」を実際に押せるようにするだけです。
 * ページ移動は当直のぶんなので管理者モードは要りません
 * (`services/nippou_service.edit_page`)。
 */
function showAllStopStuck(message, page) {
  const panel = document.getElementById("check-panel");
  const head = document.getElementById("check-head");
  const list = document.getElementById("check-list");
  if (!panel || !head || !list) { toast(message, "warn"); return; }
  head.textContent = message;
  head.className = "msg msg--warn";
  list.replaceChildren();
  const li = document.createElement("li");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn--sm";
  btn.textContent = `第${page}ページを開く`;
  btn.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/settings/page", { page });
      toast(body.message, "ok");
      location.href = body.next || "/";
    } catch (err) { toastError(err); }
  });
  li.append(btn);
  list.append(li);
  panel.hidden = false;
  panel.scrollIntoView({ behavior: "smooth", block: "center" });
}
