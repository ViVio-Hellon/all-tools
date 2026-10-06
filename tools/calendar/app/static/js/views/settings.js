/*
  views/settings.js — 設定画面

  tkinter 版で上部のボタンに散らばっていた
  [ライン設定] [同期設定] [取り込み] [今すぐ同期] をここに集めた。

  **判断はサーバが持つ。** 保存できるか・何と出すか・どんな問題があるかは
  すべて `GET /api/settings` が返してくる。ここは写して、押されたら投げるだけ。
*/

import { saveText } from "../desktop.js";
import { withPassword } from "../adminpass.js";
import { ApiError, api } from "../api.js";
import { confirm, inform, modal } from "../modal.js";
import * as sync from "../sync.js";
import { toast, toastError } from "../toast.js";
import * as master from "./master.js";
import * as theme from "../theme.js";

let state = null;

export function start() {
  bind("st-line-save", saveLine);
  bind("st-auto-save", saveAutoSync);

  // 参照パス。**2つ別々に保存する** ── 片方を直したいだけのときに、
  // もう片方まで送って上書きしないため
  bind("st-data-save", () => savePath("data_db_dir", "st-data-dir"));
  bind("st-master-save", () => savePath("master_db_dir", "st-master-dir"));
  bind("st-data-browse", () => browseInto("st-data-dir", { folder: true }));
  bind("st-master-browse", () => browseInto("st-master-dir", { folder: true }));

  bind("st-import-all", () => runImport("all", false));
  document.querySelectorAll("[data-import]").forEach((node) => {
    node.addEventListener("click", () => runImport(node.dataset.import, false));
  });
  bind("st-csv-browse", () => browseInto("st-csv"));
  bind("st-csv-run", runCsvImport);

  // 配布設定。書き出す・読み込み直す・消す(どれも管理者パスワード)
  startDistribution();

  // 9. ログ。書き先を決める・記録番号からまとめる
  bind("st-log-save", () => saveLogDir(document.getElementById("st-log-dir").value));
  bind("st-log-default", () => saveLogDir(""));
  bind("st-log-browse", () => browseInto("st-log-dir", { folder: true }));
  bind("st-log-report", () => showReport(document.getElementById("st-log-ref").value));
  bind("st-log-recent", loadRecent);

  bind("st-pw-save", () => changeAdminPassword(false));
  bind("st-pw-reset", () => changeAdminPassword(true));

  // 帯の同期状態が変わったら、この画面の表示も合わせる
  sync.subscribe(() => { if (state) load(); });

  load();
  master.start();
}

function bind(id, handler) {
  const node = document.getElementById(id);
  if (node) node.addEventListener("click", handler);
}

/* ------------------------------------------------------------------
   読み込みと描画
   ------------------------------------------------------------------ */
async function load() {
  try {
    render(await api.get("/api/settings"));
  } catch (err) {
    toastError(err);
  }
}

function render(payload) {
  state = payload;

  // 1. ライン
  const line = document.getElementById("st-line");
  // **中身で比べる。** 数だけだと、アクセス権限で選べるラインが
  // 入れ替わったとき(コイル → 作業長)に古い選択肢が残る
  const shown = [...line.options].map((o) => o.value).join("\n");
  if (shown !== payload.line_options.join("\n")) {
    line.replaceChildren(...payload.line_options.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      return option;
    }));
  }
  line.value = payload.my_line || "";
  // どう切り替えるか。**表で決まっていればパスワードは要らない**
  const access = document.getElementById("st-line-access");
  if (access && payload.access) {
    access.textContent = payload.access.explain;
    access.className = "note note--" + (payload.access.managed ? "ok" : "info");
  }
  // **ライン固定なら選ばせない。** 押せる形のまま断ると、押し間違いに見える
  const fixed = !!(payload.access && payload.access.fixed);
  line.disabled = fixed;
  const lineSave = document.getElementById("st-line-save");
  if (lineSave) lineSave.disabled = fixed;
  const ribbonLine = document.getElementById("rb-line");
  if (ribbonLine) ribbonLine.textContent = payload.my_line || "未設定";

  // 2. 参照パス
  setValue("st-data-dir", payload.data_db_dir);
  setValue("st-master-dir", payload.master_db_dir);
  // **欄が空でも動いていることがある**(環境変数の既定で)。
  // 何も出さないと「未設定なのに読めている」ように見えるので、添え書きで言う
  withDefault("st-data-dir", payload.data_db_dir_default);
  withDefault("st-master-dir", payload.master_db_dir_default);
  note("st-data-note", payload.data_db_path, payload.data_db_note);
  note("st-master-note", payload.master_db_path, payload.master_db_note);
  setValue("st-auto", payload.auto_sync ? "1" : "0");
  setValue("st-interval", payload.sync_interval);

  const pill = document.getElementById("st-sync-pill");
  pill.textContent = payload.sync_text;
  pill.className = "sub pill pill--"
    + (payload.sync_state === "offline" ? "error"
       : payload.sync_state === "pending" ? "warn"
       : payload.sync_state === "disabled" ? "info" : "ok");

  const transport = document.getElementById("st-transport");
  transport.textContent = payload.transport_note;
  transport.className = "note note--" + (payload.can_auto_sync ? "info" : "warn");

  // 3. 班員名簿
  const members = document.getElementById("st-members");
  members.textContent = payload.member_count
    ? `班員名簿 ${payload.member_count} 件`
    : "班員名簿がありません";
  members.className = "sub pill pill--" + (payload.member_count ? "ok" : "warn");

  // 5. 管理者パスワード。**値は来ない。** 変えてあるかどうかだけ
  const pwPill = document.getElementById("st-pw-pill");
  if (pwPill) {
    pwPill.textContent = payload.admin_custom
      ? "この端末で変えてあります" : "既定のまま";
    pwPill.className = "sub pill pill--" + (payload.admin_custom ? "ok" : "info");
  }
  const pwFile = document.getElementById("st-pw-file");
  if (pwFile) pwFile.textContent = payload.settings_file || "";

  // 6. 配布設定。**パスワードの値は来ない。**「(設定済み)」とだけ
  renderDistribution(payload.distribution);

  // 直すべきこと。**いちばん上にまとめて出す**
  const problems = document.getElementById("st-problems");
  problems.replaceChildren(...payload.problems.map((text) => {
    const note = document.createElement("div");
    note.className = "note note--warn";
    note.textContent = text;
    return note;
  }));

  // 9. ログ
  renderLog(payload.log || {});

  // 10. 画面の見た目
  renderTheme(payload.theme_choices || []);

  // 7. ファイルの置き場所。**まとまりごとに**出す(この端末だけ / 共有 / アプリ)
  renderStorage(payload.storage || []);

  // 8. いまの状態
  const status = document.getElementById("st-status");
  status.replaceChildren();
  [["版", payload.version],
   ["アプリ本体", payload.app_root],
   ["データ", payload.data_dir],
   ["ログ", payload.log_dir],
   ["配布設定", payload.distribution.exists
     ? `あり(${payload.distribution.created_at} に ${payload.distribution.created_on} で作成)`
     : "なし"],
   ["配布設定の読み込み", payload.distribution.applied_at || "(まだ)"],
   ["最終送信", payload.last_sent_at || "(まだ)"],
   ["最終取り込み", payload.last_received_at || "(まだ)"],
   ["送信待ち", `${payload.pending} 件`],
  ].forEach(([key, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    status.append(dt, dd);
  });
}

/* ------------------------------------------------------------------
   配布設定(`calendar_app/distribution.py`)
   ------------------------------------------------------------------ */
function renderDistribution(dist) {
  const pill = document.getElementById("st-dist-state");
  pill.textContent = dist.exists ? "あり" : "なし";
  pill.className = `sub pill pill--${dist.exists ? "ok" : "warn"}`;
  document.getElementById("st-dist-meta").textContent = dist.exists
    ? `${dist.created_at} に ${dist.created_on} で作成(VER${dist.version})`
    : "まだありません。下で書き出すと、ツールのフォルダの直下に「配布設定」フォルダができます。";
  document.getElementById("st-dist-rows").replaceChildren(
    ...dist.contents.flatMap((c) => {
      const dt = document.createElement("dt");
      dt.textContent = c.label;
      const dd = document.createElement("dd");
      dd.textContent = c.value;
      return [dt, dd];
    }));
  document.getElementById("st-dist-path").textContent = dist.path;

  // 入れる項目。**一度だけ作る** ── 描き直すたびに作ると、選び直した
  // チェックが帯の更新のたびに既定へ戻る
  const box = document.getElementById("st-dist-items");
  if (!box.querySelector("[data-dist-item]")) {
    box.append(...dist.items.map((it) => {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.distItem = it.key;
      input.checked = it.default;
      label.append(input, ` ${it.label}`);
      return label;
    }));
  }
}

function startDistribution() {
  const password = document.getElementById("st-dist-password");
  const why = document.getElementById("st-dist-why");
  const send = async (path, body) => {
    why.hidden = true;
    try {
      const payload = await api.post(path, { ...body, password: password.value });
      password.value = "";
      render(payload);
      toast(payload.message || "済みました", "ok");
      sync.refresh();
      master.refresh();
    } catch (err) {
      // 断りの理由はサーバが持っている。押した場所のそばに出す
      why.hidden = false;
      why.textContent = err instanceof ApiError ? err.message : String(err);
    }
  };
  const checked = () => [...document.querySelectorAll("[data-dist-item]")]
    .filter((node) => node.checked)
    .map((node) => node.dataset.distItem);
  bind("st-dist-export", () => send("/api/settings/distribution/export",
                                    { items: checked() }));
  bind("st-dist-reapply", () => send("/api/settings/distribution/reapply", {}));
  bind("st-dist-remove", () => send("/api/settings/distribution/remove", {}));
}

function renderStorage(items) {
  const host = document.getElementById("st-storage");
  if (!host) return;
  const groups = [];
  items.forEach((item) => {
    let group = groups.find((g) => g.name === item.group);
    if (!group) { group = { name: item.group, items: [] }; groups.push(group); }
    group.items.push(item);
  });
  host.replaceChildren(...groups.map((group) => {
    const box = document.createElement("div");
    box.className = "store";
    const head = document.createElement("h3");
    head.className = "store__head";
    head.textContent = group.name;
    box.appendChild(head);
    group.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "store__item";
      const title = document.createElement("div");
      title.className = "store__name";
      const name = document.createElement("b");
      name.textContent = item.name;
      title.appendChild(name);
      if (item.keep) {
        const pill = document.createElement("span");
        pill.className = "pill pill--warn";
        pill.textContent = "消さない";
        title.appendChild(pill);
      }
      if (!item.exists) {
        const none = document.createElement("span");
        none.className = "pill pill--info";
        none.textContent = "まだ無い";
        title.appendChild(none);
      }
      const path = document.createElement("code");
      path.className = "store__path";
      path.textContent = item.path;
      const holds = document.createElement("div");
      holds.textContent = item.holds;
      const note = document.createElement("div");
      note.className = "store__note";
      note.textContent = item.note;
      row.append(title, path, holds, note);
      box.appendChild(row);
    });
    return box;
  }));
}

/** 見つかったファイルと、開いてみた結果。**exists ではなく開けたか**を出す。 */
function note(id, path, text) {
  const node = document.getElementById(id);
  if (!node) return;
  node.textContent = path ? `${path} — ${text}` : text;
}

/** 既定(環境変数)があれば、空欄の添え書きにする。無ければ元の添え書きのまま。 */
function withDefault(id, fallback) {
  const node = document.getElementById(id);
  if (!node) return;
  if (!node.dataset.hint) node.dataset.hint = node.placeholder || "";
  node.placeholder = fallback
    ? `既定: ${fallback}(空欄のままならこれを使います)`
    : node.dataset.hint;
}

function setValue(id, value) {
  const node = document.getElementById(id);
  // **入力中の欄は書き換えない。** 帯の同期状態が更新されるたびに
  // 描き直すので、打っている途中の文字が消えてしまう
  if (node && document.activeElement !== node) node.value = value;
}

/* ------------------------------------------------------------------
   保存

   ライン設定と参照パスは**管理者パスワードで守られている**
   (`calendar_app/admin_password.py`)。聞き方と送り直し方は
   `adminpass.js` に1つだけ置いてある ── マスタ確認の「次のライン」も
   同じ関門を通るので、2か所に持つと必ず片方が古くなる。
   ------------------------------------------------------------------ */
async function post(path, body) {
  const result = await withPassword((sending) => api.post(path, sending), body);
  if (result.ok) {
    render(result.payload);
    toast(result.payload.message, "ok");
    sync.refresh();
    return result.payload;
  }
  if (result.cancelled) return null;

  const err = result.error;
  if (result.exhausted) {
    await inform("管理者パスワードが確認できません", err.message);
    return null;
  }
  if (err instanceof ApiError && (err.status === 422 || err.status === 409)) {
    await inform("保存できません", err.message);
    return null;
  }
  toastError(err);
  return null;
}

const saveLine = () =>
  post("/api/settings/line", { line: document.getElementById("st-line").value });

async function savePath(field, inputId) {
  const payload = await post("/api/settings/paths",
                             { [field]: document.getElementById(inputId).value });
  // 参照パスが変わるとマスタ管理の可否も変わる
  if (payload) master.refresh();
}

/* ------------------------------------------------------------------
   10. 画面の見た目。選べるものはサーバが返す。いまどれかは `theme.js` が持つ
   (帯のボタンで変えたあとも、この画面の印が合うように)
   ------------------------------------------------------------------ */
function renderTheme(choices) {
  const box = document.getElementById("st-theme");
  if (!box) return;
  if (!box.querySelector("input")) {
    box.append(...choices.map((choice) => {
      const label = document.createElement("label");
      label.className = "theme-choice";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "theme";
      input.value = choice.value;
      input.addEventListener("change", () => { if (input.checked) theme.choose(choice.value); });
      const text = document.createElement("span");
      text.textContent = choice.label;
      label.append(input, text);
      return label;
    }));
    // 帯のボタンで変えたときも、ここの印を合わせる
    document.addEventListener("app:theme", () => markTheme(box));
  }
  markTheme(box);
}

function markTheme(box) {
  box.querySelectorAll("input").forEach((input) => {
    input.checked = input.value === theme.chosen();
  });
}

/* ------------------------------------------------------------------
   9. ログ(エラーの後追い)

   **まとめるのはサーバ**(`calendar_app/log_report.py`)。ここは番号を
   送って、返ってきた文をそのまま見せるだけ。
   ------------------------------------------------------------------ */
function renderLog(log) {
  const input = document.getElementById("st-log-dir");
  // 打ちかけを描き直しで消さない
  if (input && document.activeElement !== input) input.value = log.setting || "";
  const pill = document.getElementById("st-log-pill");
  if (pill) {
    pill.textContent = log.fallback_reason ? "指定先に書けません"
      : log.setting ? "指定した場所" : "既定の場所";
    pill.className = "sub pill pill--" + (log.fallback_reason ? "warn" : "ok");
  }
  const note = document.getElementById("st-log-note");
  if (note) {
    const lines = [`いまの書き先: ${log.folder || "(なし)"}`];
    if (log.fallback_reason) lines.push(`指定先に書けないので既定の場所へ書いています: ${log.fallback_reason}`);
    lines.push(`${log.keep_days || 30}日より古いログは消えます。`);
    note.textContent = lines.join("\n");
    note.style.whiteSpace = "pre-wrap";
  }
}

async function saveLogDir(value) {
  try {
    const payload = await api.post("/api/settings/log-dir", { log_dir: value });
    render(payload);
    toast(payload.message, payload.log && payload.log.fallback_reason ? "warn" : "ok");
  } catch (err) {
    if (err instanceof ApiError && (err.status === 422 || err.status === 400)) {
      await inform("保存できません", err.message);
      return;
    }
    toastError(err);
  }
}

async function loadRecent() {
  const host = document.getElementById("st-log-list");
  try {
    const data = await api.get("/api/logs");
    renderRecent(host, data);
  } catch (err) {
    toastError(err);
  }
}

function renderRecent(host, data) {
  if (!data.errors.length) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = `最近${data.days}日に記録番号の付いた記録はありません。`;
    host.replaceChildren(p);
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "mtable";
  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["日時", "記録番号", "どこで", "何が起きたか", ""].forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    head.appendChild(th);
  });
  const thead = document.createElement("thead");
  thead.appendChild(head);
  const tbody = document.createElement("tbody");
  data.errors.forEach((row) => {
    const tr = document.createElement("tr");
    [row.at, row.ref, row.where, row.what].forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(td);
    });
    const td = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn";
    button.textContent = "まとめる";
    button.addEventListener("click", () => showReport(row.ref));
    td.appendChild(button);
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
  wrap.appendChild(table);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = `最近${data.days}日・新しい順(${data.errors.length}件)。`;
  host.replaceChildren(hint, wrap);
}

async function showReport(ref) {
  const wanted = String(ref || "").trim().toUpperCase();
  if (!wanted) {
    await inform("記録番号が要ります", "画面に出た記録番号(E で始まる番号)を入れてください。");
    return;
  }
  let data;
  try {
    data = await api.get(`/api/logs/report?ref=${encodeURIComponent(wanted)}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      await inform("見つかりません", err.message);
      return;
    }
    toastError(err);
    return;
  }
  let area = null;
  await modal({
    title: `なぜなぜ分析の材料 ── ${data.ref}`,
    wide: true,
    render(body) {
      area = document.createElement("textarea");
      area.className = "input report-text";
      area.readOnly = true;
      area.value = data.text;
      area.rows = 24;
      body.appendChild(area);
      // **頭から読ませる。** 開いたときに末尾(なぜなぜの空欄)が出ると、
      // 「何が起きたか」を読まずに埋めはじめる
      requestAnimationFrame(() => { area.scrollTop = 0; area.setSelectionRange(0, 0); });
    },
    actions: [
      { label: "写す", value: () => { copyText(area); return undefined; } },
      { label: "ファイルに保存", value: () => { saveReport(data); return undefined; } },
      { label: "閉じる", value: true, kind: "primary" },
    ],
  });
}

async function copyText(area) {
  try {
    await navigator.clipboard.writeText(area.value);
  } catch {
    // 使えないブラウザでは選んで写す
    area.select();
    document.execCommand("copy");
  }
  toast("写しました。報告書や表計算に貼り付けられます。", "ok");
}

async function saveReport(data) {
  // デスクトップ版は保存先を訊く窓(外枠)、ブラウザ版はダウンロード(`desktop.js`)
  try {
    if (await saveText(`なぜなぜ材料_${data.ref}.txt`, data.text)) {
      toast("保存しました。", "ok");
    }
  } catch (err) {
    toastError(err);
  }
}

/* ------------------------------------------------------------------
   管理者パスワードを変える

   **値は画面に持たない。** 打った3つを送って、返ってきた状態を写すだけ。
   合っているかどうかの判断はサーバにしかない(`admin_password.py`)。
   ------------------------------------------------------------------ */
async function changeAdminPassword(reset) {
  const ids = ["st-pw-now", "st-pw-new", "st-pw-confirm"];
  const [now, next, confirmValue] = ids.map(
    (id) => document.getElementById(id).value);
  const body = reset
    ? { current: now, reset: true }
    : { current: now, new: next, confirm: confirmValue };
  try {
    const payload = await api.post("/api/settings/admin-password", body);
    render(payload);
    // 打った値は残さない。肩越しに見られる時間を短くする
    ids.forEach((id) => { document.getElementById(id).value = ""; });
    toast(payload.message, "ok");
  } catch (err) {
    // 断りの理由はサーバが持っている。押した場所のそばに出す
    const why = document.getElementById("st-pw-why");
    why.hidden = false;
    why.textContent = err instanceof ApiError ? err.message : String(err);
  }
}

const saveAutoSync = () =>
  post("/api/settings/auto-sync", {
    enabled: document.getElementById("st-auto").value === "1",
    interval: Number(document.getElementById("st-interval").value),
  });

/* ------------------------------------------------------------------
   取り込み
   ------------------------------------------------------------------ */
async function runImport(target, force) {
  try {
    const payload = await api.post("/api/import", { target, force });
    render(payload);
    toast(payload.message, "ok");
    sync.refresh();
    master.refresh();
  } catch (err) {
    // 409 = 未送信の入力がある。**破棄してよいかは利用者が決める**
    if (err instanceof ApiError && err.code === "pending_changes") {
      const ok = await confirm("未反映の変更があります",
        `${err.message}\n\n${err.hint}\n`
        + "(取り込みはテーブルを入れ替えるため、まだ取り込み元へ送れていない入力は消えます)",
        { okLabel: "破棄して取り込む", danger: true });
      if (ok) await runImport(target, true);
      return;
    }
    if (err instanceof ApiError && err.status === 422) {
      await inform("取り込めません", err.message);
      return;
    }
    toastError(err);
  }
}

async function runCsvImport() {
  const path = document.getElementById("st-csv").value.trim();
  if (!path) {
    await inform("パスが要ります",
                 "CSV のパスを入力するか、[参照] で選んでください。");
    return;
  }
  try {
    const payload = await api.post("/api/import/csv", { path });
    render(payload);
    toast(payload.message, "ok");
  } catch (err) {
    if (err instanceof ApiError && err.status === 422) {
      await inform("取り込めません", err.message);
      return;
    }
    toastError(err);
  }
}

/* ------------------------------------------------------------------
   フォルダ参照 (tkinter 版 filedialog.askopenfilename の置き換え)

   ブラウザのファイル選択は**中身**しか渡さない。要るのは
   「このPCから見た共有フォルダのパス」なので、サーバ側を歩く。
   ------------------------------------------------------------------ */
async function browseInto(targetId, opts = {}) {
  const target = document.getElementById(targetId);
  const chosen = await browse(target.value.trim(), opts);
  if (chosen) {
    target.value = chosen;
    target.focus();
  }
}

/**
 * @param {string} startPath
 * @param {{folder?: boolean}} opts `folder` なら**フォルダを選ばせる**
 *        (参照パスはフォルダで持つので、ファイルを押しても親を返す)
 */
function browse(startPath, opts = {}) {
  return modal({
    title: opts.folder ? "フォルダを選ぶ" : "ファイルを選ぶ",
    wide: true,
    render(body, close) {
      const box = document.createElement("div");
      box.className = "browse";
      const path = document.createElement("div");
      path.className = "browse__path";
      const list = document.createElement("div");
      list.className = "browse__list";
      box.append(path, list);

      const message = document.createElement("p");
      message.className = "hint";

      const jump = document.createElement("div");
      jump.className = "row";
      const input = document.createElement("input");
      input.className = "input";
      input.placeholder = "\\\\サーバ名\\共有  (パスを直接入力もできます)";
      input.spellcheck = false;
      const go = document.createElement("button");
      go.className = "btn";
      go.type = "button";
      go.textContent = "開く";
      jump.append(input, go);

      // フォルダを選ぶときは、いま開いている場所をそのまま返せるようにする
      let choose = null;
      if (opts.folder) {
        choose = document.createElement("button");
        choose.className = "btn btn--primary";
        choose.type = "button";
        choose.textContent = "このフォルダにする";
        jump.appendChild(choose);
      }

      body.append(jump, box, message);

      async function show(where) {
        let view;
        try {
          view = await api.post("/api/settings/browse", { path: where });
        } catch (err) {
          toastError(err);
          return;
        }
        path.textContent = view.path || "(場所を選んでください)";
        message.textContent = view.message || "";
        input.value = view.path || "";

        const rows = [];
        if (view.parent) {
          rows.push(row("&#8593;", "上のフォルダへ", "dir", () => show(view.parent)));
        }
        view.dirs.forEach((entry) => {
          rows.push(row("&#128193;", entry.name, "dir", () => show(entry.path)));
        });
        view.files.forEach((entry) => {
          // フォルダを選ぶ場面では、ファイルを押したら**その親**を返す。
          // 参照パスはフォルダで持つので、フルパスを返しても保存できない
          rows.push(row("&#128196;", entry.name, "file",
                        () => close(opts.folder ? view.path : entry.path)));
        });
        list.replaceChildren(...rows);
        if (choose) choose.disabled = !view.exists;
      }

      function row(glyph, label, kind, onClick) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "browse__item";
        button.dataset.kind = kind;
        const icon = document.createElement("span");
        icon.className = "gl";
        icon.innerHTML = glyph;
        const text = document.createElement("span");
        text.textContent = label;
        button.append(icon, text);
        button.addEventListener("click", onClick);
        return button;
      }

      if (choose) {
        choose.addEventListener("click", () => {
          if (path.textContent && path.textContent !== "(場所を選んでください)") {
            close(path.textContent);
          }
        });
      }
      go.addEventListener("click", () => show(input.value.trim()));
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") { event.preventDefault(); show(input.value.trim()); }
      });
      show(startPath);
    },
    actions: [{ label: "取り消し", value: null }],
  });
}
