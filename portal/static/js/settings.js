/*
  settings.js — 大設定(タブ表示権限・共有の DB の置き場所・管理者パスワード・ツールの一覧)

  書き換えは管理者パスワードで鍵を開けてから(30分さわらなければ鍵が掛かる)。
  表は共有の DB に直接書き、書いたら手元の写しも読み直す(サーバの仕事)。
  2人が同時に同じ行を直したときは、あとの人に「読み直してから」を出す(409)。
*/
import { api } from "./api.js";
import { toast, toastError } from "./toast.js";

const MARK = { nippou: "日", kanban: "看", calendar: "暦", inspection: "点" };
const PHASE_LABEL = { idle: "まだ開いていません", starting: "起動中", started: "動作中",
                      failed: "止まっています", quit: "終了しました" };

let ctx = { desktop: false, invoke: null, tools: new Map(), reloadTabs: () => {} };
let view = null;
let editing = null;      // 直している行(新しい行なら null)
let phases = new Map();
let rowBusy = false;     // 行を書いている途中(「保存する」の2度押しで同じ行を2つ足さない)
let rowBase = "";        // 行の窓を開いたときの値(打ったかどうかを見分ける)

// 問い合わせの順番。**あとから頼んだ答えを、先に頼んだ古い答えで描き直さない**
// (読み直しと書き込みが行き違うと、古い表・古い置き場所に戻って見えていた)
let asked = 0;
let shown = 0;

const $ = (id) => document.getElementById(id);

export function install(options) {
  ctx = { ...ctx, ...options };
  $("lock-open").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api.post("/api/settings/auth", { password: $("lock-pass").value });
      $("lock-pass").value = "";
      toast("鍵を開けました(30分さわらなければ掛かります)");
      await refresh();
    } catch (err) { toastError(err); }
  });
  // 行の窓を開いたまま鍵が掛かった(30分)。窓を閉じずに、その場で開けられる
  // (以前は窓の裏の欄へ案内するだけで、「やめる」で閉じるしかなく、打った行が消えていた)
  $("row-unlock-go").addEventListener("click", unlockInRow);
  $("row-unlock-pass").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); unlockInRow(); }
  });
  $("lock-close").addEventListener("click", async () => {
    try { await api.post("/api/settings/auth", { lock: true }); await refresh(); }
    catch (err) { toastError(err); }
  });
  $("rights-sync").addEventListener("click", async () => {
    try {
      const seq = ++asked;
      const body = await api.post("/api/rights/sync", {});
      paintIfFresh(seq, body);
      const s = body.sync || {};
      toast(s.error ? s.error : (s.message || "読み直しました"), s.error ? "ng" : "ok");
      ctx.reloadTabs();
    } catch (err) { toastError(err); }
  });
  $("rights-create").addEventListener("click", () => write("/api/rights/create-table", {}));
  $("rights-add").addEventListener("click", () => openRow(null));
  // 「やめる」・Esc: 打った値があれば訊いてから閉じる
  $("row-cancel").addEventListener("click", () => closeRow());
  $("row-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeRow();
  });
  $("row-form").addEventListener("submit", saveRow);
  for (const button of document.querySelectorAll("[data-fill]")) {
    button.addEventListener("click", () => {
      const me = view?.identity || {};
      if (button.dataset.fill === "login") $("row-login").value = me.login_id || "";
      else $("row-pc").value = me.pc_name || "";
    });
  }
  for (const button of document.querySelectorAll("[data-copy]")) {
    button.addEventListener("click", () => copy($(button.dataset.copy).textContent));
  }
  // 画面の色の既定。変えたら大きなタブの画面と4ツールの枠へその場で渡す(`shell.js`)
  for (const button of document.querySelectorAll("[data-theme-default]")) {
    button.addEventListener("click", async () => {
      const theme = button.dataset.themeDefault;
      if (await write("/api/settings/theme", { theme })) {
        window.dispatchEvent(new CustomEvent("alltools:theme-default", { detail: theme }));
      }
    });
  }
  // ブラウザ版: 画面が開かなかったときに待つ分
  $("first-contact-form").addEventListener("submit", (event) => {
    event.preventDefault();
    write("/api/settings/first-contact", { minutes: $("first-contact-min").value });
  });
  $("first-contact-default").addEventListener("click", () => {
    $("first-contact-min").value = "5";
    write("/api/settings/first-contact", { minutes: 5 });
  });
  $("loc-form").addEventListener("submit", (event) => {
    event.preventDefault();
    write("/api/settings/location", { folder: $("loc-folder").value, name: $("loc-name").value },
          { location: true });
  });
  $("loc-default").addEventListener("click", () => write("/api/settings/location", { folder: "", name: "" },
                                                          { location: true }));
  for (const id of ["loc-folder", "loc-name"]) $(id).addEventListener("input", paintLocState);
  $("dist-export").addEventListener("click", () => {
    // 書き出すのは**反映している**置き場所。打ちかけ(まだ「変える」を押していない値)が
    // あるのに書き出すと、打った値は配られず、本人は配ったつもりになる
    if (locEdited()) {
      toast("共有の DB の置き場所に、まだ反映していない値があります。先に「変える」で反映するか、"
        + "元に戻してから書き出してください。", "ng", 9000);
      $("loc-folder").focus();
      return;
    }
    write("/api/distribution/export", {});
  });
  $("dist-reapply").addEventListener("click", () => {
    if (confirm("配布設定を読み込み直します。この端末の置き場所・管理者パスワードは、配布設定の値で上書きされます。よろしいですか?")) {
      write("/api/distribution/reapply", {});
    }
  });
  $("dist-remove").addEventListener("click", () => {
    if (confirm("配布設定(一式のフォルダの直下の「配布設定」)を消します。この端末の設定はそのままです。よろしいですか?")) {
      write("/api/distribution/remove", {});
    }
  });
  $("pw-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const body = await api.post("/api/settings/password", {
        current: $("pw-current").value, new: $("pw-new").value, confirm: $("pw-confirm").value });
      for (const id of ["pw-current", "pw-new", "pw-confirm"]) $(id).value = "";
      toast(body.message);
      await refresh();
    } catch (err) { toastError(err); }
  });
}

/** 大設定のタブを開いたとき */
export function opened() {
  refresh().catch(toastError);
}

export function decisionChanged(decision) {
  if (view && decision) {
    view.decision = decision;
    paintMe();
  }
}

export function statusChanged(list) {
  phases = new Map(list.map((item) => [item.id, item.phase]));
  if (view) paintTools();
}

/**
 * 閉じる前(窓の × ・「終了」・ブラウザ版の「終了」)に見る、大設定の打ちかけ。
 * 大きなタブの画面(shell.js)が、各ツールに訊く前にこれを訊く。
 */
export function unsaved() {
  const found = [];
  if (locEdited()) found.push("共有の DB の置き場所(打った値をまだ「変える」で反映していません)");
  if (rowEdited()) found.push("タブ表示権限の行(打った値をまだ保存していません)");
  if (["pw-current", "pw-new", "pw-confirm"].some((id) => $(id) && $(id).value)) {
    found.push("管理者パスワードの変更(まだ「変える」を押していません)");
  }
  return found;
}

/** 打ちかけの中身(閉じる確認で「閉じる」を選んだあと、同じ中身なら2度訊かないため) */
export function unsavedStamp() {
  return JSON.stringify([$("loc-folder")?.value, $("loc-name")?.value, rowEdited() ? rowValues() : "",
                         ["pw-current", "pw-new", "pw-confirm"].map((id) => ($(id) ? $(id).value.length : 0))]);
}

/** 入口の処理(Python)が止まった・起こし直した(外枠が知らせる)。大設定の上に出す */
export function portalState(kind, text = "") {
  const box = $("portal-state");
  if (!box) return;
  if (kind === "lost") {
    box.hidden = false;
    box.className = "msg msg--warn portal-state";
    box.textContent = "日報複合ツールの入口の処理(Python)が止まったので、起こし直しています。"
      + "各タブの画面はそのままです(読み直していません)。この画面の打ちかけも残っています。";
  } else if (kind === "back") {
    box.hidden = false;
    box.className = "msg msg--info portal-state";
    box.textContent = "日報複合ツールの入口の処理(Python)を起こし直しました。管理者の鍵は掛かり直しています。"
      + "打ちかけの値はそのまま残してあります。";
    refresh().catch(() => {});
  } else if (kind === "down") {
    box.hidden = false;
    box.className = "msg msg--warn portal-state";
    box.textContent = "日報複合ツールの入口の処理(Python)を起こし直せませんでした。"
      + (text ? `(${text})` : "") + "各タブの画面はそのまま使えますが、大設定は使えません。"
      + "「もう一度開く」で起こし直すか、打ちかけを保存してから日報複合ツールを開き直してください。";
    if (ctx.desktop && ctx.invoke) {
      const again = document.createElement("button");
      again.type = "button";
      again.className = "btn btn--small";
      again.textContent = "もう一度開く";
      again.addEventListener("click", () => ctx.invoke("shell_restart_tool", { tool: "portal" })
        .then(() => portalState("lost")).catch(toastError));
      box.append(" ", again);
    }
  } else {
    box.hidden = true;
  }
}

/** 頼んだ順番が古くなければ描く。古い答え(あとから頼んだ答えが先に描かれた)は捨てる */
function paintIfFresh(seq, body, options = {}) {
  if (seq < shown) {
    // 描かないが、置き場所を「変える」で反映したことだけは欄に返す(打ちかけの印を消す)
    if (options.location && body && body.source) resetLocation(body.source);
    return false;
  }
  shown = seq;
  paint(body, options);
  return true;
}

async function refresh() {
  const seq = ++asked;
  const body = await api.get("/api/settings");
  paintIfFresh(seq, body);
}

/**
 * 書き込み。`options.location`: 置き場所を反映した(成功したら欄を反映した値に戻す)。
 * `options.onError(err)`: 断られたときに自分で手当てする(true を返せば、ここでは出さない)。
 */
async function write(path, payload, options = {}) {
  try {
    const seq = ++asked;
    const body = await api.post(path, payload);
    paintIfFresh(seq, body, options);
    toast(body.message || "反映しました");
    ctx.reloadTabs();
    return true;
  } catch (err) {
    if (options.onError && options.onError(err)) return false;
    if (err.status === 403 && err.code === "locked") {
      toast("管理者パスワードで鍵を開けてください(打った値はそのまま残っています)", "ng");
      $("lock-pass").focus();
    } else if (err.status === 409) {
      toastError(err);
      if (err.body && err.body.rules) paintIfFresh(++asked, err.body);
      else await refresh().catch(() => {});
    } else {
      toastError(err);
    }
    return false;
  }
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast(`写しました: ${text}`); }
  catch (err) { window.prompt("この文を写してください", text); }
}

// ------------------------------------------------------------------
// 描く
// ------------------------------------------------------------------
function paint(body, options = {}) {
  view = body;
  for (const button of document.querySelectorAll("[data-theme-default]")) {
    button.setAttribute("aria-pressed", String(button.dataset.themeDefault === (body.theme_default || "light")));
  }
  // 打ちかけ(入力中)は上書きしない
  const waitBox = $("first-contact-min");
  if (waitBox && document.activeElement !== waitBox) waitBox.value = String(body.first_contact_min || 5);
  const box = $("lockbox");
  box.dataset.open = body.admin ? "1" : "0";
  $("lock-state").textContent = body.admin ? "🔓 鍵が開いています" : "🔒 鍵が掛かっています";
  $("lock-open").hidden = Boolean(body.admin);
  $("lock-close").hidden = !body.admin;
  for (const node of document.querySelectorAll("[data-admin]")) {
    node.setAttribute("aria-disabled", body.admin ? "false" : "true");
    node.title = body.admin ? "" : "管理者パスワードで鍵を開けると使えます";
  }
  paintMe();
  paintRights();
  paintSource(Boolean(options.location));
  paintTools();
  paintDistribution();
  paintApp();
  $("pw-lead").textContent = body.password_custom
    ? "この端末で変えたパスワードが効いています。変えるには、いまのパスワードが要ります(4文字以上)。"
    : "まだ変えていません(ほかのツールと同じ既定のパスワード)。変えるには、いまのパスワードが要ります(4文字以上)。";
}

function chips(ids) {
  const wrap = document.createElement("span");
  wrap.className = "chips";
  for (const id of ids) {
    const tool = ctx.tools.get(id);
    if (!tool) continue;
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.dataset.mark = MARK[id] || tool.title.slice(0, 1);
    chip.style.setProperty("--c", `var(--tool-${id})`);
    chip.textContent = tool.title;
    wrap.append(chip);
  }
  return wrap;
}

function paintMe() {
  const me = view.identity || {};
  $("me-login").textContent = me.login_id || "(不明)";
  $("me-pc").textContent = me.pc_name || "(不明)";
  const d = view.decision || {};
  const tabs = $("me-tabs");
  tabs.replaceChildren(d.tabs && d.tabs.length ? chips(d.tabs) : document.createTextNode("(ツールのタブは出していません)"));
  $("me-reason").textContent = d.reason || "";
  const warn = $("me-warn");
  if (d.source === "unregistered") {
    warn.hidden = false;
    warn.textContent = `この端末は登録されていません。登録するには「+ 行を足す」で、PC名 ${me.pc_name || ""}`
      + `(またはログインID ${me.login_id || ""})の行を作ってください。`;
  } else if (d.source === "unreadable") {
    warn.hidden = false;
    warn.textContent = "共有フォルダにつながる状態で「共有から読み直す」を押すと、タブ表示権限で決め直します。";
  } else if (d.source === "unconfigured") {
    warn.hidden = false;
    warn.textContent = "まだ運用を始めていません。1行でも足すと、登録の無い端末にはツールのタブが出なくなります"
      + "(大設定だけになります)。先に、使う端末をまとめて登録してください。";
  } else {
    warn.hidden = true;
  }
}

function time(sec) {
  if (!sec) return "まだ";
  const d = new Date(sec * 1000);
  return d.toLocaleString("ja-JP", { hour12: false });
}

function paintSource(reset = false) {
  const s = view.source || {};
  const line = $("rights-source");
  line.replaceChildren();
  const parts = [
    ["置き場所", s.path],
    ["決めたところ", s.origin],
    ["最後に読めた", time(s.imported_at)],
  ];
  for (const [label, value] of parts) {
    const span = document.createElement("span");
    span.innerHTML = "";
    const b = document.createElement("b");
    b.textContent = `${label}: `;
    span.append(b, document.createTextNode(value || "―"), document.createTextNode("　"));
    line.append(span);
  }
  if (s.error) {
    const err = document.createElement("div");
    err.className = "msg msg--warn";
    err.textContent = s.imported_at
      ? `${s.error}(前回読めた手元の写しで決めています)`
      : `${s.error}(手元の写しも無いので、すべてのタブを出しています)`;
    line.append(err);
  }
  $("rights-missing").hidden = s.state !== "missing";
  $("rights-table").textContent = s.table || "タブ表示権限";
  // いま反映している値を覚えておき、打ちかけと見分ける(`paintLocState`)。
  //
  //     打った値が勝手に戻ることがありました
  //
  // 以前は描くたびに欄を反映済みの値で上書きしていた ── 鍵を開けた・共有から読み直した・
  // タブを行き来した・1分ごとのタブの見直し…のたびに、打ちかけの置き場所が消えていた。
  // とんでもない話である。**打ちかけの欄には触らない。** 欄を反映した値に戻すのは、
  // 「変える」「既定に戻す」が通ったときだけ(`reset`)。
  //
  // 欄に入れるのは**大設定で決めた値**(`set_*`)。効いている既定(環境変数・既定)を
  // 入れると、ほかを直して「変える」を押したときに既定が大設定の値として書き込まれていた
  for (const [id, value, fallback] of [["loc-folder", s.set_folder ?? s.folder, s.default_folder],
                                       ["loc-name", s.set_name ?? s.name, s.default_name]]) {
    const input = $(id);
    const saved = value || "";
    const typed = input.dataset.saved !== undefined && input.value.trim() !== input.dataset.saved;
    if (reset || !typed) input.value = saved;
    input.dataset.saved = saved;
    input.placeholder = fallback ? `空なら既定: ${fallback}` : "";
  }
  $("loc-note").textContent = `既定: ${s.default_folder}\\${s.default_name}`;
  paintLocState();
}

/** 置き場所を反映した(「変える」「既定に戻す」が通った)。欄を反映した値に戻す */
function resetLocation(s) {
  for (const [id, value] of [["loc-folder", s.set_folder ?? s.folder], ["loc-name", s.set_name ?? s.name]]) {
    $(id).value = value || "";
    $(id).dataset.saved = value || "";
  }
  paintLocState();
}

/** 置き場所の欄に、まだ反映していない値があるか */
function locEdited() {
  return ["loc-folder", "loc-name"].some((id) => {
    const input = $(id);
    return input && input.dataset.saved !== undefined && input.value.trim() !== input.dataset.saved;
  });
}

/** 共有の DB の置き場所: **反映しているか、打ちかけか**を色と一言で出す。 */
function paintLocState() {
  const s = view?.source || {};
  let edited = false;
  for (const id of ["loc-folder", "loc-name"]) {
    const input = $(id);
    const changed = input.value.trim() !== (input.dataset.saved || "");
    input.classList.toggle("is-edited", changed);
    edited = edited || changed;
  }
  const state = $("loc-state");
  state.dataset.state = edited ? "edited" : "saved";
  state.textContent = edited
    ? "まだ反映していません ── 「変える」を押すと、この場所を使います"
    : `反映しています: ${s.path || "―"}${!(s.set_folder ?? s.folder) && !(s.set_name ?? s.name)
      ? (s.origin === "環境変数" ? "(環境変数の場所)" : "(既定の場所)") : ""}`;
}

function paintRights() {
  const body = $("rights-grid").querySelector("tbody");
  body.replaceChildren();
  const me = view.identity || {};
  const rules = view.rules || [];
  const fold = (t) => String(t || "").normalize("NFKC").toLowerCase().trim();
  if (!rules.length) {
    const tr = document.createElement("tr");
    tr.className = "empty";
    const td = document.createElement("td");
    td.colSpan = 8;
    td.textContent = view.source?.state === "ok" ? "まだ1行もありません" : "表を読めていません";
    tr.append(td);
    body.append(tr);
  }
  for (const rule of rules) {
    const tr = document.createElement("tr");
    const mine = rule.enabled && (rule.login_id || rule.pc_name)
      && (!rule.login_id || fold(rule.login_id) === fold(me.login_id))
      && (!rule.pc_name || fold(rule.pc_name) === fold(me.pc_name));
    if (mine) tr.classList.add("is-mine");
    if (!rule.enabled) tr.classList.add("is-off");
    const cells = [rule.key ?? "", rule.login_id || "(問わない)", rule.pc_name || "(問わない)"];
    for (const value of cells) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    const tabs = document.createElement("td");
    tabs.append(chips(rule.tab_ids || []));
    if (rule.unknown && rule.unknown.length) {
      const bad = document.createElement("span");
      bad.className = "state";
      bad.dataset.phase = "failed";
      bad.textContent = ` 分からない: ${rule.unknown.join("、")}`;
      tabs.append(bad);
    }
    tr.append(tabs);
    for (const value of [rule.default_tab || "", rule.enabled ? "有効" : "無効", rule.note || ""]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    const act = document.createElement("td");
    act.className = "act";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "btn btn--small";
    edit.textContent = "直す";
    edit.dataset.admin = "";
    edit.addEventListener("click", () => openRow(rule));
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn--small btn--danger";
    del.textContent = "消す";
    del.addEventListener("click", () => removeRow(rule));
    act.append(edit, " ", del);
    tr.append(act);
    body.append(tr);
  }
  const problems = $("rights-problems");
  problems.replaceChildren();
  for (const text of view.problems || []) {
    const li = document.createElement("li");
    li.textContent = text;
    problems.append(li);
  }
  problems.hidden = !(view.problems || []).length;
}

function paintTools() {
  const body = $("tools-grid").querySelector("tbody");
  body.replaceChildren();
  for (const tool of view.tools || []) {
    const tr = document.createElement("tr");
    const tab = document.createElement("td");
    tab.append(chips([tool.id]));
    const name = document.createElement("td");
    name.textContent = tool.name;
    const ver = document.createElement("td");
    ver.textContent = tool.version ? `VER${tool.version}` : "";
    const state = document.createElement("td");
    const phase = phases.get(tool.id) || (ctx.desktop ? "idle" : "");
    const s = document.createElement("span");
    s.className = "state";
    s.dataset.phase = phase;
    s.textContent = phase ? PHASE_LABEL[phase] || phase : "(ブラウザ版はタブで確かめます)";
    state.append(s);
    const local = document.createElement("td");
    const code = document.createElement("code");
    code.textContent = tool.local;
    local.append(code);
    const act = document.createElement("td");
    act.className = "act";
    if (ctx.desktop && (phase === "failed" || phase === "quit")) {
      const again = document.createElement("button");
      again.type = "button";
      again.className = "btn btn--small";
      again.textContent = "もう一度開く";
      again.addEventListener("click", () => ctx.invoke("shell_restart_tool", { tool: tool.id })
        .then(() => toast(`${tool.title} を起こし直しています`)).catch(toastError));
      act.append(again);
    }
    tr.append(tab, name, ver, state, local, act);
    body.append(tr);
  }
}

function paintDistribution() {
  const d = view.distribution || {};
  const state = $("dist-state");
  if (d.exists) {
    state.textContent = `置いてあります: ${d.path}(作成 ${d.created_at || "?"} / ${d.created_on || "?"}`
      + `${d.version ? ` / VER${d.version}` : ""})`
      + (d.applied_at ? `。この端末が最後に読み込んだ・書き出したのは ${d.applied_at}` : "");
  } else {
    state.textContent = `置いてありません(書き出すと ${d.path || "配布設定"} にできます)`;
  }
  const list = $("dist-contents");
  list.replaceChildren();
  for (const item of d.contents || []) {
    const li = document.createElement("li");
    li.textContent = `${item.label}: ${item.value}`;
    list.append(li);
  }
  $("dist-reapply").hidden = !d.exists;
  $("dist-remove").hidden = !d.exists;
  // 各ツールの配布設定(そのツールのフォルダの中)。make_dist.bat はこれをまとめて入れる
  const body = $("dist-tools")?.tBodies[0];
  if (body) {
    body.replaceChildren();
    for (const t of d.tools || []) {
      const tr = document.createElement("tr");
      const cells = [
        t.title,
        t.exists ? `あり(作成 ${t.created_at || "?"}${t.created_on ? ` / ${t.created_on}` : ""})` : "なし(書き出していません)",
        t.path,
      ];
      cells.forEach((text, i) => {
        const td = document.createElement("td");
        td.textContent = text;
        if (i === 1 && t.exists) td.className = "is-ok";
        if (i === 2) td.className = "mono";
        tr.append(td);
      });
      body.append(tr);
    }
  }
}

function paintApp() {
  const a = view.app || {};
  const dl = $("app-facts");
  dl.replaceChildren();
  const rows = [
    ["名前", `${a.name} VER${a.version}`],
    ["動かし方", ctx.desktop ? "デスクトップ版(日報複合ツール.exe)" : "ブラウザ版(予備。Start.vbs)"],
    ["一式の場所", a.root],
    ["手元の領域", a.local],
    ["ログ", a.logs],
  ];
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    const code = document.createElement("code");
    code.textContent = value || "";
    dd.append(label === "名前" || label === "動かし方" ? document.createTextNode(value || "") : code);
    dl.append(dt, dd);
  }
}

// ------------------------------------------------------------------
// 行を足す・直す・消す
// ------------------------------------------------------------------
function openRow(rule) {
  if (!view?.admin) {
    toast("管理者パスワードで鍵を開けてください", "ng");
    $("lock-pass").focus();
    return;
  }
  editing = rule;
  $("row-title").textContent = rule ? `管理番号 ${rule.key} を直す` : "タブ表示権限に行を足す";
  $("row-login").value = rule ? rule.login_id : "";
  $("row-pc").value = rule ? rule.pc_name : (view.identity?.pc_name || "");
  $("row-enabled").checked = rule ? rule.enabled : true;
  $("row-note").value = rule ? rule.note : "";
  $("row-error").hidden = true;
  $("row-unlock").hidden = true;
  const checks = $("row-tabs");
  checks.replaceChildren();
  const chosen = new Set(rule ? rule.tab_ids || [] : []);
  for (const tool of view.catalog || []) {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = tool.id;
    box.checked = chosen.has(tool.id);
    label.append(box, document.createTextNode(tool.title));
    checks.append(label);
  }
  // この版が知らない語(新しいツール・打ち間違い)は、チェックにできないので見せるだけ。
  // 保存しても消さない(サーバが表の文字のまま残す)
  if (rule && rule.unknown && rule.unknown.length) {
    const note = document.createElement("small");
    note.className = "sub";
    note.textContent = `分からない語(そのまま残します): ${rule.unknown.join("、")}`;
    checks.append(note);
  }
  const select = $("row-default");
  select.replaceChildren(new Option("(指定しない)", ""));
  for (const tool of view.catalog || []) select.append(new Option(tool.title, tool.id));
  const current = (view.catalog || []).find((t) => rule && (t.title === rule.default_tab || t.id === rule.default_tab));
  if (current) {
    select.value = current.id;
  } else if (rule && rule.default_tab) {
    // 知らない既定タブ。選べるようにしておき、そのまま残す(以前は保存で空になっていた)
    select.append(new Option(`${rule.default_tab}(分からない名前・そのまま残します)`, rule.default_tab));
    select.value = rule.default_tab;
  } else {
    select.value = "";
  }
  rowBase = rowValues();
  $("row-dialog").showModal();
}

/** 行の窓の値(打ったかどうかを見分ける) */
function rowValues() {
  return JSON.stringify([
    $("row-login").value, $("row-pc").value,
    [...$("row-tabs").querySelectorAll("input:checked")].map((b) => b.value),
    $("row-default").value, $("row-enabled").checked, $("row-note").value,
  ]);
}

/** 行の窓を開いていて、開いたときから値を変えたか */
function rowEdited() {
  return Boolean($("row-dialog")?.open) && rowValues() !== rowBase;
}

/** 「やめる」・Esc。打った値があれば訊く(以前は訊かずに捨てていた) */
function closeRow() {
  if (rowBusy) return;
  if (rowEdited() && !confirm("打った値はまだ保存していません。保存せずに閉じますか?")) return;
  $("row-dialog").close();
}

function rowMessage(text) {
  $("row-error").hidden = false;
  $("row-error").textContent = text;
}

/** 行の窓の中で鍵を開ける(窓を閉じずに。打った値はそのまま) */
async function unlockInRow() {
  try {
    await api.post("/api/settings/auth", { password: $("row-unlock-pass").value });
    $("row-unlock-pass").value = "";
    $("row-unlock").hidden = true;
    rowMessage("鍵を開けました。もう一度「保存する」を押してください。");
    await refresh();
  } catch (err) {
    toastError(err);
    $("row-unlock-pass").focus();
  }
}

/** 行を書いて断られたとき。**打った値は消さない**(窓を開いたまま、手当ての道を出す) */
function rowRefused(err) {
  if (err.status === 403 && err.code === "locked") {
    $("row-unlock").hidden = false;
    rowMessage("鍵が掛かりました(30分さわらなかったため)。打った値はそのまま残っています。"
      + "下で鍵を開けてから、もう一度「保存する」を押してください。");
    $("row-unlock-pass").focus();
    return true;
  }
  if (err.status !== 409) {
    rowMessage((err && err.message) || String(err));
    return false;
  }
  // 読み直した表が一緒に届く。描き直し、直している行の「いま」(was)だけ差し替える
  if (err.body && err.body.rules) paintIfFresh(++asked, err.body);
  if (err.code === "no_table") {
    rowMessage(err.message);
    return true;
  }
  if (editing) {
    const fresh = (view?.rules || []).find((r) => r.key === editing.key);
    if (fresh) {
      editing = fresh;
      rowMessage(`ほかの人が先にこの行を直していました(いまの表: 表示タブ「${fresh.tabs || "―"}」`
        + `・既定タブ「${fresh.default_tab || "―"}」・${fresh.enabled ? "有効" : "無効"}・備考「${fresh.note || "―"}」)。`
        + "打った値はそのまま残しています。この値で上書きするなら、もう一度「保存する」を押してください。");
    } else {
      editing = null;
      $("row-title").textContent = "タブ表示権限に行を足す(もとの行は消されていました)";
      rowMessage("その行はほかの人が消していました。打った値はそのまま残しています。"
        + "もう一度「保存する」を押すと、新しい行として足します。");
    }
    return true;
  }
  rowMessage(err.message);
  return true;
}

async function saveRow(event) {
  event.preventDefault();
  // **書いている途中は受けない**(「保存する」の2度押しで、同じ行が2つ足されていた)
  if (rowBusy) return;
  const tabIds = [...$("row-tabs").querySelectorAll("input:checked")].map((b) => b.value);
  const payload = {
    login_id: $("row-login").value, pc_name: $("row-pc").value, tab_ids: tabIds,
    default_tab: $("row-default").value, enabled: $("row-enabled").checked, note: $("row-note").value,
  };
  if (!payload.login_id.trim() && !payload.pc_name.trim()) {
    rowMessage("ログインID か PC名 の少なくとも一方を入れてください。");
    return;
  }
  // 最初の1行は、登録の無い端末からツールのタブを消す(運用の始まり)。確かめてから
  const effective = (view.rules || []).filter((r) => r.enabled && (r.login_id || r.pc_name));
  if (!editing && !effective.length && payload.enabled
      && !confirm("最初の1行です。\n\nこの行を足すと、タブ表示権限に登録の無い端末には、"
        + "次に開いたときからツールのタブが出なくなります(大設定だけになります)。\n\n続けますか?")) {
    return;
  }
  rowBusy = true;
  $("row-save").disabled = true;
  $("row-cancel").disabled = true;
  let ok = false;
  try {
    if (editing) {
      ok = await write("/api/rights/save", { ...payload, key: editing.key, was: wasOf(editing) },
                       { onError: rowRefused });
    } else {
      ok = await write("/api/rights/add", payload, { onError: rowRefused });
    }
  } finally {
    rowBusy = false;
    $("row-save").disabled = false;
    $("row-cancel").disabled = false;
  }
  if (ok) $("row-dialog").close();
}

function wasOf(rule) {
  return { login_id: rule.login_id, pc_name: rule.pc_name, tabs: rule.tabs,
           default_tab: rule.default_tab, enabled: rule.enabled, note: rule.note };
}

async function removeRow(rule) {
  if (!view?.admin) {
    toast("管理者パスワードで鍵を開けてください", "ng");
    return;
  }
  if (!confirm(`管理番号 ${rule.key}(${rule.condition})を消します。よろしいですか?`)) return;
  await write("/api/rights/delete", { key: rule.key, was: wasOf(rule) });
}
