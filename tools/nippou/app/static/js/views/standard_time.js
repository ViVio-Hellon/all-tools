/*
  views/standard_time.js — 標準作業時間

      標準作業時間は別タブ管理とし csv出力 同条件作業抽出 作業時間を表示

  面は5つ(並びはサーバ `presenters/standard_time.TABS`):
    標準(条件×班)   条件ごとの標準。行の「抽出」で同条件の作業へ
    同条件の作業     その条件の作業を1件ずつ、作業時間と標準との差つきで
    梱包力           標準を 100 とした梱包の速さ(ゲームの DPS のように)
    考え方と計算     読むだけ(サーバが描く)。ほかの面の「しくみ」からここへ飛ぶ
    過去ぶんを入れる この端末の集計から

  **表を組み立てるのはサーバ**(`presenters/standard_time.py`)。ここは
  受け取った行を器へ流し込み、ボタンを配線するだけ。

  【`start()` の中で全部やる理由】
  この画面へ2度目に来たとき、モジュールは読み直されない(ES モジュールは
  一度きり)。**要素は毎回引き直す**必要があるので、配線はここに閉じる。
*/

import { api } from "../api.js";
import { lineLabel } from "../line_label.js";
import { pageSignal } from "../nav.js";
import { paintTable } from "../table.js";
import { attachAll, current, select } from "../tabs.js";
import { toast, toastError } from "../toast.js";

const $ = (id) => document.getElementById(id);
const value = (id) => $(id)?.value || "";
/** 欄のつなぎ方(AND / OR)。**既定は AND**。 */
const joinOf = (name) =>
  document.querySelector(`input[name="${name}"]:checked`)?.value || "and";
const setJoin = (name, join) => {
  const radio = document.querySelector(`input[name="${name}"][value="${join}"]`);
  if (radio) radio.checked = true;
};

/** 知らせの箱。空なら隠す。 */
function note(id, text, kind = "info") {
  const box = $(id);
  if (!box) return;
  box.hidden = !text;
  box.textContent = text || "";
  box.className = `msg msg--${kind}`;
}

/** 班の選択肢を、サーバが返した班で埋める(選んでいたものは残す)。 */
function paintTeams(id, teams, allLabel) {
  const select = $(id);
  if (!select) return;
  const picked = select.value;
  select.replaceChildren(new Option(allLabel, ""));
  for (const team of teams || []) select.append(new Option(team, team));
  select.value = (teams || []).includes(picked) ? picked : "";
}

/**
 * CSVの出力先を、**この画面の上で**保存する。
 *
 * 保存先は設定・参照設定の「標準作業時間CSVの出力パス」と同じ設定です
 * (`/api/settings/paths`)。パスの変更には管理者パスワードが要るので、
 * 断られたら(403)その場に合言葉の欄を出して、もう一度押してもらいます
 * ── 設定画面へ行って探し直させない。
 */
function wireOutDir() {
  const box = $("std-out");
  const btn = $("std-out-save");
  if (!box || !btn) return;
  const key = box.dataset.key;
  const pw = $("std-out-pw");
  const showPw = (on) => {
    pw.hidden = !on;
    $("std-out-pw-label").hidden = !on;
    if (on) pw.focus();
  };
  btn.addEventListener("click", async () => {
    const payload = { [key]: value("std-out-dir").trim() };
    if (pw.value) payload.password = pw.value;
    try {
      const body = await api.post("/api/settings/paths", payload);
      pw.value = "";                     // 合言葉は画面に残さない
      showPw(false);
      const view = (body.paths || []).find((v) => v.key === key);
      if (view) $("std-out-path").textContent = view.resolved;
      $("std-out-same").hidden = Boolean(value("std-out-dir").trim());
      note("std-out-note", "");
      toast("CSVの出力先を保存しました", "ok");
    } catch (err) {
      if (err.status === 403) showPw(true);
      note("std-out-note", err.message, "error");
    }
  });
}

export function start() {
  attachAll();
  wireOutDir();
  const tabs = $("standardTabs");

  /* ================================================================
     標準(条件×班)
     ================================================================ */
  /* ================================================================
     作業者名で見る(v4.12.4)── **普段は出さない**。入れたときだけ、標準の面に
     「まとめ方」、梱包力のまとめ方に「作業者ごと」が出る
     ================================================================ */
  const workerMode = () => Boolean($("worker-mode")?.checked);

  function paintWorkerMode() {
    const on = workerMode();
    for (const el of document.querySelectorAll("[data-worker-only]")) {
      el.hidden = !on;
      if (el.tagName === "OPTION") el.disabled = !on;
    }
  }

  const standardSpan = () => ({
    scope: value("standard-scope") || "line",
    // 作業者ごとは「作業者名で見る」のときだけ(切ってあれば班ごと)
    group: (value("standard-group") === "worker" && !workerMode())
      ? "team" : (value("standard-group") || "team"),
    team: value("standard-team"),
    purpose_code: value("standard-purpose"),
    packing_spec_no: value("standard-spec"),
    size: value("standard-size"),
    packages: value("standard-packages"),
    workers: value("standard-workers"),
    join: joinOf("standard-join"),
    formulas: Boolean($("standard-formulas")?.checked),
  });

  async function loadStandards() {
    try {
      const view = await api.post("/api/standard-time/standards", standardSpan());
      const conditions = view.conditions || [];
      paintTable(view.table, {
        rowAction: {
          label: "抽出",
          title: (i) => "この条件の作業を1件ずつ出す",
          onClick: (i) => extract(conditions[i]),
        },
      });
      paintTeams("standard-team", view.teams, "全部");
      paintTeams("works-team", view.teams, "全班");
      const source = $("standard-source");
      if (source) {
        source.textContent = `出どころ: ${view.source}`
          + (view.samples ? ` ── 実績 ${view.samples}行(数えなかった行 ${view.excluded})` : "");
      }
      note("standard-warning", view.warning, "warn");
    } catch (err) {
      note("standard-warning", err.message, "warn");
    }
  }

  $("standard-reload")?.addEventListener("click", loadStandards);
  for (const id of ["standard-scope", "standard-team", "standard-formulas", "standard-group"]) {
    $(id)?.addEventListener("change", loadStandards);
  }
  for (const radio of document.querySelectorAll('input[name="standard-join"]')) {
    radio.addEventListener("change", loadStandards);
  }
  for (const id of ["standard-purpose", "standard-spec", "standard-size",
                    "standard-packages", "standard-workers"]) {
    $(id)?.addEventListener("change", loadStandards);
    // Enter でも引き直す(表示ボタンまで手を伸ばさせない)
    $(id)?.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); loadStandards(); }
    });
  }
  $("standard-csv")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/standard-time/standards/csv", standardSpan());
      note("standard-msg", body.message, "ok");
      toast("標準作業時間をCSVに書き出しました");
    } catch (err) {
      note("standard-msg", err.message, "error");
      toastError(err.message);
    }
  });

  /* ================================================================
     同条件の作業
     ================================================================ */
  const worksSpan = () => ({
    line: value("works-line"),
    team: value("works-team"),
    purpose_code: value("works-purpose"),
    packing_spec_no: value("works-spec"),
    size: value("works-size"),
    start: value("works-start"),
    end: value("works-end"),
    include_excluded: Boolean($("works-excluded")?.checked),
    join: joinOf("works-join"),
    formulas: Boolean($("works-formulas")?.checked),
  });

  /** 標準の行の「抽出」── 条件を入れて、面を移して、引く。 */
  function extract(cond) {
    if (!cond) return;
    const set = (id, v) => { if ($(id)) $(id).value = v || ""; };
    set("works-line", lineLabel(cond.line));     // 見せるのは正規の呼び名(サーバはどちらも受ける)
    set("works-purpose", cond.purpose_code);
    set("works-spec", cond.packing_spec_no);
    set("works-size", cond.size);
    // 標準の1行 = 3つとも同じ条件。**AND に戻す**(OR のままだと別の条件まで混ざる)
    setJoin("works-join", "and");
    const team = $("works-team");
    if (team) {
      const want = cond.team === "全班" ? "" : cond.team;
      if (want && ![...team.options].some((o) => o.value === want)) {
        team.append(new Option(want, want));
      }
      team.value = want;
    }
    if (tabs) select(tabs, "works");
    loadWorks();
  }

  function paintSummary(summary) {
    const box = $("works-summary");
    if (!box) return;
    box.replaceChildren();
    // 並びはサーバが決めた順(件数 → 作業時間 → 標準との差)
    for (const [label, text] of summary || []) {
      const item = document.createElement("div");
      item.className = "fact";
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = text;
      item.append(dt, dd);
      box.append(item);
    }
  }

  async function loadWorks() {
    try {
      const view = await api.post("/api/standard-time/works", worksSpan());
      paintTable(view.table);
      paintSummary(view.summary);
      if ($("works-standard")) $("works-standard").textContent = view.standard || "";
      if ($("works-source")) $("works-source").textContent = `出どころ: ${view.source}`;
      note("works-warning", view.warning, "warn");
      note("works-msg", "");
    } catch (err) {
      note("works-warning", err.message, "warn");
    }
  }

  $("works-run")?.addEventListener("click", loadWorks);
  // 条件が入っているときだけ、選び直したらそのまま引き直す
  const hasCriteria = () => value("works-purpose") || value("works-spec") || value("works-size");
  for (const id of ["works-team", "works-excluded", "works-formulas"]) {
    $(id)?.addEventListener("change", () => { if (hasCriteria()) loadWorks(); });
  }
  for (const radio of document.querySelectorAll('input[name="works-join"]')) {
    radio.addEventListener("change", () => { if (hasCriteria()) loadWorks(); });
  }
  for (const id of ["works-line", "works-purpose", "works-spec", "works-size"]) {
    $(id)?.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); loadWorks(); }
    });
  }
  $("works-csv")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/standard-time/works/csv", worksSpan());
      note("works-msg", body.message, "ok");
      toast("同条件の作業をCSVに書き出しました");
    } catch (err) {
      note("works-msg", err.message, "error");
      toastError(err.message);
    }
  });

  /* ================================================================
     梱包力 ── 面を開いたときに初めて引く(開かない人に待たせない)
     ================================================================ */
  const powerSpan = () => ({
    scope: value("power-scope") || "line",
    team: value("power-team"),
    group: value("power-group") || "team",
    start: value("power-start"),
    end: value("power-end"),
    formulas: Boolean($("power-formulas")?.checked),
  });
  let powerLoaded = false;

  /** DPS メーターのような棒。**幅も値もサーバが決めたまま**描く。 */
  function paintMeter(view) {
    const list = $("power-meter");
    if (!list) return;
    list.replaceChildren();
    const rows = view.meter || [];
    list.hidden = !rows.length;
    if (!rows.length) return;
    list.style.setProperty("--ref", `${view.scale.ref}%`);
    // 目盛りの行: 100 の線に「標準 100」と書く
    const axis = document.createElement("li");
    axis.className = "power-meter__axis";
    axis.setAttribute("aria-hidden", "true");
    const tick = document.createElement("span");
    tick.className = "power-meter__tick";
    tick.textContent = "標準 100";
    const track = document.createElement("span");
    track.className = "power-meter__track";
    track.append(tick);
    axis.append(document.createElement("span"), track, document.createElement("span"));
    list.append(axis);
    for (const row of rows) {
      const li = document.createElement("li");
      li.className = "power-meter__row" + (row.muted ? " is-muted" : "");
      li.dataset.hint = row.hint;
      li.setAttribute("aria-label", row.hint.split("\n")[0]);
      const label = document.createElement("span");
      label.className = "power-meter__label";
      label.textContent = row.label;
      const bar = document.createElement("span");
      bar.className = "power-meter__track";
      const fill = document.createElement("span");
      fill.className = "power-meter__fill";
      fill.style.width = `${row.width}%`;
      bar.append(fill);
      const num = document.createElement("span");
      num.className = "power-meter__value";
      num.textContent = row.value;
      li.append(label, bar, num);
      list.append(li);
    }
  }

  function paintPower(view) {
    const hero = view.hero || {};
    if ($("power-value")) $("power-value").textContent = hero.value || "—";
    if ($("power-meaning")) $("power-meaning").textContent = hero.meaning || "";
    if ($("power-compared")) $("power-compared").textContent = hero.compared || "";
    const box = $("power-hero");
    if (box) {
      // 式は乗せたときに(1行目に出すと長い)
      if (hero.formula) box.dataset.hint = hero.formula;
      else delete box.dataset.hint;
    }
    paintMeter(view);
    const more = $("power-more");
    if (more) {
      more.hidden = !view.more_note;
      more.textContent = view.more_note || "";
    }
    paintTable(view.table);
    paintTeams("power-team", view.teams, "全班");
    if ($("power-source")) $("power-source").textContent = `出どころ: ${view.source}`;
    note("power-warning", view.warning, "warn");
  }

  async function loadPower() {
    powerLoaded = true;
    try {
      paintPower(await api.post("/api/standard-time/power", powerSpan()));
      note("power-msg", "");
    } catch (err) {
      note("power-warning", err.message, "warn");
    }
  }

  const signal = pageSignal();
  $("worker-mode")?.addEventListener("change", () => {
    paintWorkerMode();
    // 切ったら、作業者ごとで出していた表を班ごとへ戻す(名前を出したまま残さない)
    if (!workerMode()) {
      if (value("standard-group") === "worker") {
        $("standard-group").value = "team";
        loadStandards();
      }
      if (value("power-group") === "worker") {
        $("power-group").value = "team";
        if (powerLoaded) loadPower();
      }
    }
  });
  paintWorkerMode();
  const shown = (key) => { if (key === "power" && !powerLoaded) loadPower(); };
  tabs?.addEventListener("tab:select", (ev) => shown(ev.detail.key), { signal });
  $("power-reload")?.addEventListener("click", loadPower);
  for (const id of ["power-scope", "power-team", "power-group", "power-start", "power-end",
                    "power-formulas"]) {
    $(id)?.addEventListener("change", loadPower);
  }
  $("power-csv")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/standard-time/power/csv", powerSpan());
      note("power-msg", body.message, "ok");
      toast("梱包力をCSVに書き出しました");
    } catch (err) {
      note("power-msg", err.message, "error");
      toastError(err.message);
    }
  });

  /* ================================================================
     過去ぶんを入れる
     ================================================================ */
  $("backfill-run")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/standard-time/backfill", {
        start: value("backfill-start"), end: value("backfill-end"),
      });
      note("backfill-note", body.message, body.error ? "warn" : body.shifts ? "ok" : "info");
      if (body.shifts) loadStandards();
    } catch (err) {
      note("backfill-note", err.message, "error");
    }
  });

  /* ================================================================
     考え方と計算へ ── `data-goto-tab` のリンクは、面を開いてから飛び先へ
     (閉じた面の中へは、ブラウザだけでは飛べない)
     ================================================================ */
  document.addEventListener("click", (ev) => {
    const link = ev.target.closest?.("a[data-goto-tab]");
    if (!link || !tabs) return;
    ev.preventDefault();
    select(tabs, link.dataset.gotoTab);
    const id = new URL(link.href, location.href).hash.slice(1);
    const target = id && document.getElementById(id);
    if (target) target.scrollIntoView({ block: "start" });
  }, { signal });

  loadStandards();
  if (tabs) shown(current(tabs));
}
