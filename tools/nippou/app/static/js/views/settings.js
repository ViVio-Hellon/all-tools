/*
  views/settings.js — 設定・管理者

  どの操作も「押す → サーバへ投げる → 文言を出す」だけ。
  **押せるかどうかの判断はサーバが持つ**(管理者かどうか等)。
*/
import { api, background } from "../api.js";
import * as desktop from "../desktop.js";
import { lineLabel } from "../line_label.js";
import { toast, toastError } from "../toast.js";
import { refresh } from "../nav.js";
import { loadCues, previewFile } from "../sound.js";
// 面(タブ)の作りは全画面で1つ。**並びと鍵の要否はサーバが決める**
import { attachAll } from "../tabs.js";
import { watch as watchJob } from "../progress.js";

/* ---- 小さな道具 ------------------------------------------------- */
const byId = (id) => document.getElementById(id);

function note(text, kind = "info") {
  const box = document.getElementById("access-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}


/* ================================================================
   ページ移動 (VBA 管理者フレームの spnPage / btnGoToPage)

   **押せるかどうかもサーバが決める。** ここは返ってきた状態を写すだけ
   ── 「保存データがあるか」「管理者か」を画面が自分で判断すると、
   同じ規則が2か所に散る。
   ================================================================ */
/* ================================================================
   DB保存済み一覧 (VBA NippouDB_FillDBList) と当直の復旧
   ================================================================ */
function dbNote(text, kind = "info") {
  const box = document.getElementById("db-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

/* ================================================================
   過去の日報をCSVから取り込む

   **押すまで書かない。** 下見(`preview`)で何が起きるかを出し、
   「入れる」を押して初めて書きます ── 取り込みは今あるものを黙って
   置き換えられる操作なので、置き換わるページは数ではなく**一覧で**出します。

   判断はサーバが持ちます。ここは返ってきたものを並べるだけ。
   ================================================================ */
function importNote(text, kind = "info") {
  const box = document.getElementById("import-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

/**
 * 下見(または結果)の中身を、渡された箱へ描く。
 *
 * **箱を受け取る形にしてあります** ── 何本かまとめて渡されたときは
 * ファイルごとに1つずつ並べるので、描き先が1か所に固定できません。
 */
function paintPlanInto(box, plan, result) {
  if (!plan) return;

  const sheet = showSheet(plan);
  if (sheet) {
    const note = document.createElement("p");
    note.className = "lead";
    note.textContent = sheet;
    box.append(note);
  }

  const head = document.createElement("p");
  head.className = "lead";
  head.textContent = (result && result.message) || plan.message || "";
  box.append(head);

  if (plan.targets?.length) {
    /*
      表そのものが何なのかを、表の上に1行で書く。

      「入る先：何してるのかよくわかりません」と言われたところです。
      見出しが「入る先」の2文字だけだと、**何の一覧なのか**が分かり
      ません ── 読む人はまだ、このCSVが何ページぶんなのかも知らない
      ところです。

      重複したときにどうなるかも、ここで言います(「重複だった場合
      どうなるんですか？」)。**まるごと入れ替え**なので、押す前に
      知っていないと困ります。
    */
    const about = document.createElement("p");
    about.className = "lead";
    about.innerHTML =
      "このCSVの中身を、<b>どの日報として入れるか</b>の一覧です"
      + "(1行が紙1ページぶん)。<br>"
      + "<b>新しく入る</b> … その日報は手元にまだありません。そのまま足します<br>"
      + "<b>置き換え</b> … 同じ 日付・ライン・直・ページ が既にあります。"
      + "<b>中身をまるごと入れ替えます</b>"
      + "(いまある行は消えて、CSVのほうだけが残ります)";
    box.append(about);

    const table = document.createElement("table");
    table.className = "list";
    const thead = document.createElement("thead");
    thead.innerHTML =
      "<tr><th>入れる日報(日付・ライン・直・ページ)</th>"
      + "<th class='num'>入る行数</th><th>手元にあるか</th></tr>";
    const body = document.createElement("tbody");
    for (const target of plan.targets) {
      const tr = document.createElement("tr");
      const where = document.createElement("td");
      where.textContent = target.label;
      const rows = document.createElement("td");
      rows.className = "num";
      rows.textContent = target.rows;
      const state = document.createElement("td");
      if (target.replaces) {
        // **数で言わずに、どのページかを言う。** 消えるほうの行数も添える
        state.innerHTML =
          `<b>置き換え</b>(いま ${target.existing_rows}行)`;
      } else {
        state.textContent = "新しく入る";
      }
      tr.append(where, rows, state);
      body.append(tr);
    }
    table.append(thead, body);
    const scroll = document.createElement("div");
    scroll.className = "scroll-x";
    scroll.append(table);
    box.append(scroll);
  }

  if (plan.problems?.length) {
    const list = document.createElement("ul");
    list.className = "check__list";
    // 全部は出さない。**始めの数件で、何が起きているかは分かる**
    for (const problem of plan.problems.slice(0, 10)) {
      const li = document.createElement("li");
      li.textContent = `${problem.row}行目: ${problem.reason}`;
      list.append(li);
    }
    if (plan.problems.length > 10) {
      const li = document.createElement("li");
      li.textContent = `… ほか ${plan.problems.length - 10}行`;
      list.append(li);
    }
    const title = document.createElement("p");
    title.className = "lead";
    title.textContent = "入らない行:";
    box.append(title, list);
  }
}

/** 道を打って渡したときの下見。**押す前に、何が起きるかを全部出す。** */
function paintPlan(plan) {
  const box = document.getElementById("import-plan");
  if (!box) return;
  box.replaceChildren();
  box.hidden = !plan;
  if (!plan) return;
  paintPlanInto(box, plan);

  if (plan.ok) {
    const row = document.createElement("div");
    row.className = "btn-row";
    const go = document.createElement("button");
    go.className = "btn btn--primary";
    go.type = "button";
    go.id = "import-apply";
    go.textContent = plan.replacing
      ? `入れる(${plan.replacing}ページを置き換える)`
      : "入れる";
    row.append(go);
    box.append(row);
    go.addEventListener("click", () => applyImport());
  }
}

function importPath() {
  return (document.getElementById("import-path")?.value || "").trim();
}

function importLine() {
  return document.getElementById("import-line")?.value || "";
}

/*
  シートに書いてあるラインを見せて、選ぶ欄に当たりを入れる。

  **上書きはしません。** 人がもう選んでいるなら、そちらを尊重します ──
  当たりで黙って書き換えると、選んだつもりの先と違うところへ入ります。
*/
function showSheet(plan) {
  const pick = document.getElementById("import-line");
  if (pick && !pick.value && plan?.line) pick.value = plan.line;
  if (!plan?.sheet) return "";
  const parts = [];
  if (plan.sheet.report_date) parts.push(`作業日 ${plan.sheet.report_date}`);
  if (plan.sheet.line_text) parts.push(`シートのライン「${plan.sheet.line_text}」`);
  if (plan.line) parts.push(`→ ${lineLabel(plan.line)} に入れます`);
  return parts.join("  /  ");
}

/* ================================================================
   進み具合 ── 取り込んでいるあいだ、棒を伸ばす

       件数が多いときはプログレス表示させてください

   取り込みは**終わるまで返りません**(途中まで入った日報を残さない
   ため)。そのあいだ画面は押したまま止まって見えます ── 止まって
   いるのか動いているのかが分からないと、人はもう一度押すか、閉じます。

   **数を持つのはサーバ**(`nippou/job_progress.py`)。ここは 0.4 秒
   ごとに見に行って、返ってきた字をそのまま出すだけです ── 何ページ
   あるのか、いまどの段かを画面が数え直すと、必ずずれます。
   ================================================================ */
const PROGRESS_MS = 400;
let progressTimer = 0;

function paintProgress(now) {
  const box = document.getElementById("import-progress");
  if (!box) return;
  const running = Boolean(now && now.running);
  box.hidden = !running;
  if (!running) return;
  const head = document.getElementById("import-progress-head");
  const bar = document.getElementById("import-progress-bar");
  const note = document.getElementById("import-progress-note");
  if (head) head.textContent = now.headline || "";
  if (note) note.textContent = now.note || "";
  if (bar) {
    // **総数が分からないうちは、止まった棒を出さない。**
    // `removeAttribute("value")` でブラウザ既定の「動いている棒」になります
    if (now.total > 0) bar.value = now.percent;
    else bar.removeAttribute("value");
  }
}

/** そのあいだだけ見に行く。**必ず止める**(棒が出たままになるので)。 */
function watchProgress(on) {
  if (on) {
    if (progressTimer) return;
    paintProgress({ running: true, headline: "取り込みを始めています",
                    note: "", total: 0 });
    progressTimer = setInterval(async () => {
      try {
        // 誰も押していない通信。**待機の姿を出さない**(`background`)
        paintProgress(await background.get("/api/settings/import/progress"));
      } catch { /* 読めなくても取り込みは進んでいます */ }
    }, PROGRESS_MS);
    return;
  }
  clearInterval(progressTimer);
  progressTimer = 0;
  paintProgress(null);
}

async function applyImport() {
  watchProgress(true);
  try {
    const body = await api.post("/api/settings/import/apply", {
      path: importPath(),
      line: importLine(),
      mark_synced: shareChoice(),
    });
    paintPlan(null);
    // **入れたぶんは「共有へ未送信」に積まれます。** 同じ画面に出ている
    // その数が増えないままだと、取り込んだのに送る物が無いように見えます
    // ── 数を描いているのはサーバなので、描き直してもらいます
    toast(body.message, body.failed?.length ? "warn" : "ok");
    refresh();
  } catch (err) {
    // 422 は断り(読めない)。中身が返ってくるので、そのまま出す
    if (err.status === 422 && err.body) paintPlan(err.body);
    importNote(err.body?.message || err.message, "error");
  } finally {
    watchProgress(false);               // **必ず止める**(棒が残ります)
  }
}

/* ---- 掴んで落とす / まとめて渡す ----------------------------------
   ブラウザは落とされたファイルの**本当の道を教えません**(安全のため)。
   なので道ではなく**中身**を送ります ── サーバは手元へ一旦置いてから、
   道で渡すのと同じ道筋に載せます。 */
let picked = [];                       // いま渡されているファイル

function shareChoice() {
  const on = document.querySelector('[name="import-share"]:checked');
  // 既定は「そのままにする」= 共有へは書きに行かない(送信済みとして入れる)
  return (on?.value || "keep") === "keep";
}

function showPicked() {
  const label = document.getElementById("import-picked");
  if (label) {
    label.textContent = picked.length
      ? `${picked.length}本: ` + picked.map((f) => f.name).join(" / ")
      : "";
  }
  // **押すものを、選ぶものの並びの中に置く。** 下見の一番下にしか
  // 無かったので、「そのあとは？」と探させていました
  const go = document.getElementById("import-go");
  const why = document.getElementById("import-go-why");
  const path = importPath();
  const ready = picked.length > 0 || path !== "";
  if (go) {
    go.disabled = !ready;
    go.textContent = picked.length > 1
      ? `${picked.length}本を取り込む` : "取り込む";
  }
  if (why) {
    why.textContent = ready
      ? "押すと入ります(入る前に下見の結果が出ます)"
      : "上でファイルを渡すと押せます";
  }
}

function takeFiles(list) {
  picked = [...list].filter((f) => f && f.name);
  showPicked();
  paintPlan(null);
  if (picked.length) sendFiles(true);   // 落としたら、まず下見
}

function importForm(dryRun) {
  const form = new FormData();
  for (const file of picked) form.append("files", file, file.name);
  form.append("line", importLine());
  form.append("dry_run", dryRun ? "1" : "0");
  form.append("mark_synced", shareChoice() ? "1" : "0");
  return form;
}

/* ================================================================
   ライン毎目標(45度線)を、書き換えたCSVから読む

   【見本を出しても、本物にする道が無かった】
   見本は、いまのファイルがあると隣に `.見本.csv` として出ます。
   書き換えたあと、それを効かせる口が画面のどこにもありませんでした
   ── 利用者の言葉:「見本を出す意味がないよ」。

       見本を出す → 数字を書き換える → ここで渡す → 効く

   **渡しただけでは書きません。** 何ラインぶん入るのかを先に出して、
   「決定」で初めて書き換えます ── 書き換える先は共有に置いた1つの
   ファイルで、押した瞬間に全端末のグラフの目標線が変わります。
   ================================================================ */

/** いま渡されているファイル。決定を押すまで持っておく。 */
let targetFile = null;

function targetNote(text, kind = "info") {
  const box = document.getElementById("line-target-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text || "";
  box.className = `msg msg--${kind}`;
}

/** 下見の中身。**何が入るのかを、押す前に全部出す。** */
function paintTargetPreview(body) {
  const box = document.getElementById("line-target-preview");
  const row = document.getElementById("line-target-apply-row");
  if (!box) return;
  box.replaceChildren();
  box.hidden = false;
  box.className = "msg msg--info";

  const head = document.createElement("p");
  head.innerHTML = `<b>${body.file}</b> を読みました。`
    + `この値で <code>${body.dest}</code> を書き換えます:`;
  box.appendChild(head);

  const list = document.createElement("ul");
  list.className = "check__list";
  for (const r of body.rows || []) {
    const li = document.createElement("li");
    li.textContent = `${lineLabel(r.line)} … ${r.text} 枚`;
    list.appendChild(li);
  }
  box.appendChild(list);

  // **読めなかった行も出す。** 黙って飛ばすと、打ち間違いに気づけない
  if ((body.problems || []).length) {
    const bad = document.createElement("p");
    bad.innerHTML = "<b>読めなかった行:</b>";
    box.appendChild(bad);
    const ul = document.createElement("ul");
    ul.className = "check__list";
    for (const p of body.problems) {
      const li = document.createElement("li");
      li.textContent = (p.line_no ? `${p.line_no}行目: ${p.text} ── ` : "")
        + p.reason;
      ul.appendChild(li);
    }
    box.appendChild(ul);
  }
  if (row) row.hidden = false;
}

function clearTargetPreview() {
  targetFile = null;
  const box = document.getElementById("line-target-preview");
  const row = document.getElementById("line-target-apply-row");
  const input = document.getElementById("line-target-file");
  if (box) { box.hidden = true; box.replaceChildren(); }
  if (row) row.hidden = true;
  // **同じファイルをもう一度渡せるように。** 値を残すと `change` が
  // 飛ばず、2回目に選んでも何も起きないように見える
  if (input) input.value = "";
}

/**
 * 目標の表を、いまの値で描き直す。
 *
 * **画面ごと取り直しません。** `refresh()` を呼ぶと面が組み直され、
 * 開いていた「ライン毎目標」の面は既定の面に戻り、たったいま出した
 * 「読み直しました」も一緒に消えます ── 押した人には**何も起きて
 * いないように見えます**。入れ替えるのは中身だけにします。
 */
function paintTargets(view) {
  if (!view) return;
  const body = document.querySelector("#line-targets table.list tbody");
  if (body) {
    body.replaceChildren();
    for (const r of view.rows || []) {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      name.textContent = r.line;
      const value = document.createElement("td");
      if (r.text) {
        value.innerHTML = `<b></b> 枚`;
        value.querySelector("b").textContent = r.text;
      } else {
        const lead = document.createElement("span");
        lead.className = "lead";
        lead.textContent = "未設定(このラインには目標線を引きません)";
        value.appendChild(lead);
      }
      tr.append(name, value);
      body.appendChild(tr);
    }
    // **知らないライン名も残す。** 打ち間違いはここでしか気づけない
    for (const r of view.unknown || []) {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      name.innerHTML = `<code></code> <span class="badge badge--todo">知らないライン名</span>`;
      name.querySelector("code").textContent = r.line;
      const value = document.createElement("td");
      value.className = "lead";
      value.textContent = `${r.text} 枚 ── どのラインにも当たりません`;
      tr.append(name, value);
      body.appendChild(tr);
    }
  }

  const where = document.querySelector("#line-targets .path__now");
  if (where) {
    const code = where.querySelector("code");
    if (code) code.textContent = view.source;
    const badge = where.querySelector(".badge");
    if (badge) {
      badge.className = `badge badge--${view.exists ? "done" : "todo"}`;
      badge.textContent = view.exists
        ? `あります(${view.count}ライン)`
        : "まだありません(目標線は出ません)";
    }
  }
}

async function sendTargetFile(apply) {
  if (!targetFile) { targetNote("先にファイルを渡してください", "warn"); return; }
  const form = new FormData();
  form.append("file", targetFile, targetFile.name);
  if (apply) form.append("apply", "1");
  try {
    const body = await api.send("/api/settings/line-targets/upload", form);
    if (apply) {
      toast(body.message, "ok");
      clearTargetPreview();
      paintTargets(body.targets);      // 入った値をその場で見せる
      targetNote(body.message, "ok");
      return;
    }
    paintTargetPreview(body);
    targetNote(body.message, body.problems.length ? "warn" : "info");
  } catch (err) {
    clearTargetPreview();
    targetNote(err.message, "error");
  }
}

function wireTargetFile() {
  const input = document.getElementById("line-target-file");
  input?.addEventListener("change", () => {
    targetFile = input.files && input.files[0];
    if (!targetFile) { clearTargetPreview(); return; }
    sendTargetFile(false);
  });
  document.getElementById("line-target-apply")
    ?.addEventListener("click", () => sendTargetFile(true));
  document.getElementById("line-target-cancel")
    ?.addEventListener("click", () => {
      clearTargetPreview();
      targetNote("やめました。ファイルはそのままです。", "info");
    });
}

/**
 * いまのCSVを読み直して、表を描き直す。
 *
 * メモ帳で直したあと、**効いたかどうかをその場で確かめる**ための口。
 * グラフを開けばどのみち読み直されますが、そこまで行かないと分からない
 * のでは「直したつもり」で終わります。
 */
function wireTargetReload() {
  document.getElementById("line-target-reload")
    ?.addEventListener("click", async () => {
      try {
        const body = await api.post("/api/settings/line-targets/reload", {});
        // **描き直すのは中身だけ。** 画面ごと取り直すと、開いていた面も
        // いま出した文言も消えて、押しても何も起きないように見える
        paintTargets(body.targets);
        targetNote(body.message,
                   body.targets.error || !body.targets.exists ? "warn" : "ok");
      } catch (err) { targetNote(err.message, "error"); }
    });
}

/* ================================================================
   停止内訳(停止内訳.csv)── **鍵は要りません**

   「停止内訳も管理者以外が触れるようにしたい」。見本を出す → 書き換える
   → 渡す → 下見 → 決定、がライン毎目標と同じ一周です。判断(分類ごとに
   CSVかマスタか・何が増えて何が消えるか)はサーバで、ここは描くだけ。
   ================================================================ */

/** いま渡されているファイル。決定を押すまで持っておく。 */
let stopFile = null;

function stopNote(text, kind = "info") {
  const box = document.getElementById("stop-reason-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text || "";
  box.className = `msg msg--${kind}`;
}

/** 行ごとの困りごとを箇条書きに。 */
function problemList(items) {
  const ul = document.createElement("ul");
  ul.className = "check__list";
  for (const p of items || []) {
    const li = document.createElement("li");
    li.textContent = (p.line_no ? `${p.line_no}行目: ${p.text} ── ` : "") + p.reason;
    ul.appendChild(li);
  }
  return ul;
}

/** 一覧を、いまの値で描き直す。**画面ごと取り直さない**(開いた面が戻るので)。 */
function paintStopReasons(view) {
  if (!view) return;
  const body = document.getElementById("stop-reason-body");
  if (body) {
    body.replaceChildren();
    for (const g of view.groups || []) {
      const tr = document.createElement("tr");
      const head = document.createElement("td");
      head.innerHTML = "<b></b> <span></span><br><span class=\"lead\"></span>";
      head.querySelector("b").textContent = g.number;
      head.querySelector("span").textContent = g.call;
      head.querySelector(".lead").textContent = `${g.table}・記号は${g.kind}`;
      const origin = document.createElement("td");
      origin.textContent = g.origin || "―";
      if (g.origin === "CSV" && g.master_count) {
        const note = document.createElement("span");
        note.className = "lead";
        note.textContent = `(表の ${g.master_count}件は使っていません)`;
        origin.append(document.createElement("br"), note);
      }
      if (g.master_error && g.origin !== "CSV") {
        const badge = document.createElement("span");
        badge.className = "badge badge--todo";
        badge.textContent = "表を読めません";
        origin.append(document.createElement("br"), badge);
      }
      const list = document.createElement("td");
      for (const r of g.reasons || []) {
        const div = document.createElement("div");
        div.innerHTML = "<code></code> <span></span>";
        div.querySelector("code").textContent = r.code;
        div.querySelector("span").textContent = r.label;
        list.appendChild(div);
      }
      if (!(g.reasons || []).length) {
        const lead = document.createElement("span");
        lead.className = "lead";
        lead.textContent = "ありません(この分類は選べません)";
        list.appendChild(lead);
      }
      tr.append(head, origin, list);
      body.appendChild(tr);
    }
  }
  const source = document.getElementById("stop-reason-source");
  if (source) source.textContent = view.source;
  const exists = document.getElementById("stop-reason-exists");
  if (exists) {
    exists.className = `badge badge--${view.exists ? "done" : "todo"}`;
    exists.textContent = view.exists ? "あります" : "まだありません(表をそのまま使っています)";
  }
  const error = document.getElementById("stop-reason-error");
  if (error) {
    error.hidden = !view.error;
    error.textContent = view.error ? `このファイルを読めませんでした: ${view.error}` : "";
  }
  for (const [id, items, title] of [
    ["stop-reason-problems", view.problems, "使えなかった行があります。"],
    ["stop-reason-warnings", view.warnings, "使っていますが、確かめてください。"],
  ]) {
    const box = document.getElementById(id);
    if (!box) continue;
    box.hidden = !(items || []).length;
    box.replaceChildren();
    if (box.hidden) continue;
    const b = document.createElement("b");
    b.textContent = title;
    box.append(b, problemList(items));
  }
}

/** 下見。**何が増え・消え・名前が変わるかを、押す前に全部出す。** */
function paintStopPreview(body) {
  const box = document.getElementById("stop-reason-preview");
  const row = document.getElementById("stop-reason-apply-row");
  if (!box) return;
  box.replaceChildren();
  box.hidden = false;
  const diff = body.diff || {};
  const removed = diff.removed || [];
  box.className = `msg msg--${removed.length ? "warn" : "info"} msg--prose`;

  const head = document.createElement("p");
  head.innerHTML = "<b></b> を読みました。この一覧で <code></code> を書き換えます:";
  head.querySelector("b").textContent = body.file;
  head.querySelector("code").textContent = body.dest;
  box.appendChild(head);

  const groups = document.createElement("ul");
  groups.className = "check__list";
  for (const g of body.groups || []) {
    const li = document.createElement("li");
    li.textContent = g.from_master
      ? `${g.number} ${g.call} … このファイルに行がありません(表のまま)`
      : `${g.number} ${g.call} … ${g.count}件`;
    groups.appendChild(li);
  }
  box.appendChild(groups);

  const sections = [
    ["増える理由", diff.added, (d) => `${d.code} ${d.label}(${d.category})`],
    ["消える理由(昔の日報にこの記号があれば「内訳にない記号」になります)",
     removed, (d) => `${d.code} ${d.label}(${d.category})`],
    ["名前が変わる理由", diff.renamed, (d) => `${d.code} ${d.before} → ${d.label}`],
    ["分類が変わる理由", diff.moved, (d) => `${d.code} ${d.label}: ${d.before} → ${d.category}`],
  ];
  let changed = false;
  for (const [title, items, text] of sections) {
    if (!(items || []).length) continue;
    changed = true;
    const p = document.createElement("p");
    p.innerHTML = "<b></b>";
    p.querySelector("b").textContent = `${title}: ${items.length}件`;
    const ul = document.createElement("ul");
    ul.className = "check__list";
    for (const d of items) {
      const li = document.createElement("li");
      li.textContent = text(d);
      ul.appendChild(li);
    }
    box.append(p, ul);
  }
  if (!changed) {
    const p = document.createElement("p");
    p.textContent = "いまの一覧と同じです(増える・消える理由はありません)。";
    box.appendChild(p);
  }
  for (const [title, items] of [["使えない行(書き換えても入りません)", body.problems],
                                ["注意", body.warnings]]) {
    if (!(items || []).length) continue;
    const p = document.createElement("p");
    p.innerHTML = "<b></b>";
    p.querySelector("b").textContent = title;
    box.append(p, problemList(items));
  }
  if (row) row.hidden = false;
}

function clearStopPreview() {
  stopFile = null;
  const box = document.getElementById("stop-reason-preview");
  const row = document.getElementById("stop-reason-apply-row");
  const input = document.getElementById("stop-reason-file");
  if (box) { box.hidden = true; box.replaceChildren(); }
  if (row) row.hidden = true;
  // 同じファイルをもう一度渡せるように(値を残すと `change` が飛ばない)
  if (input) input.value = "";
}

async function sendStopFile(apply) {
  if (!stopFile) { stopNote("先にファイルを渡してください", "warn"); return; }
  const form = new FormData();
  form.append("file", stopFile, stopFile.name);
  if (apply) form.append("apply", "1");
  try {
    const body = await api.send("/api/settings/stop-reasons/upload", form);
    if (apply) {
      toast("停止内訳を書き換えました", "ok");
      clearStopPreview();
      paintStopReasons(body.reasons);
      stopNote(body.message, "ok");
      return;
    }
    paintStopPreview(body);
    stopNote(body.message, (body.problems || []).length ? "warn" : "info");
  } catch (err) {
    clearStopPreview();
    stopNote(err.message, "error");
  }
}

function wireStopReasons() {
  const input = document.getElementById("stop-reason-file");
  input?.addEventListener("change", () => {
    stopFile = input.files && input.files[0];
    if (!stopFile) { clearStopPreview(); return; }
    sendStopFile(false);
  });
  document.getElementById("stop-reason-apply")
    ?.addEventListener("click", () => sendStopFile(true));
  document.getElementById("stop-reason-cancel")
    ?.addEventListener("click", () => {
      clearStopPreview();
      stopNote("やめました。ファイルはそのままです。", "info");
    });
  document.getElementById("stop-reason-template")
    ?.addEventListener("click", async () => {
      try {
        const body = await api.post("/api/settings/stop-reasons/template", {});
        paintStopReasons(body.reasons);
        stopNote(body.message, "ok");
        toast("見本を出しました", "ok");
      } catch (err) { stopNote(err.message, "error"); }
    });
  document.getElementById("stop-reason-reload")
    ?.addEventListener("click", async () => {
      try {
        const body = await api.post("/api/settings/stop-reasons/reload", {});
        paintStopReasons(body.reasons);
        const v = body.reasons;
        stopNote(body.message, v.error || (v.problems || []).length ? "warn" : "ok");
      } catch (err) { stopNote(err.message, "error"); }
    });
}

/** 渡された全部を1回で。**1本ずつ押させない。** */
async function sendFiles(dryRun) {
  if (!picked.length) {
    importNote("先にファイルを渡してください", "warn");
    return;
  }
  // 下見(`dryRun`)は読むだけですが、何本もあると時間がかかるので
  // 同じように棒を出します ── 押してから何も変わらない時間を作らない
  watchProgress(true);
  try {
    const body = await api.send("/api/settings/import/upload",
                                importForm(dryRun));
    paintFiles(body, dryRun);
    importNote(body.message, body.files.some((f) => !f.ok) ? "warn" : "ok");
    if (!dryRun) { picked = []; showPicked(); }
  } catch (err) {
    paintPlan(null);
    importNote(err.message, "error");
  } finally {
    watchProgress(false);               // **必ず止める**(棒が残ります)
  }
}

/** 何本かまとめて渡したときの下見/結果。**1本ずつ見出しを付ける。** */
function paintFiles(body, dryRun) {
  const box = document.getElementById("import-plan");
  if (!box) return;
  box.replaceChildren();
  box.hidden = false;

  for (const one of body.files || []) {
    const head = document.createElement("p");
    head.className = "lead";
    head.innerHTML = `<b>${one.name}</b>`;
    box.append(head);
    if (one.error) {
      const bad = document.createElement("p");
      bad.className = "lead";
      bad.textContent = one.error;
      box.append(bad);
      continue;
    }
    const inner = document.createElement("div");
    box.append(inner);
    paintPlanInto(inner, one.preview, one.result);
  }

  if (dryRun && (body.files || []).some((f) => f.ok)) {
    const go = document.createElement("button");
    go.className = "btn btn--primary";
    go.type = "button";
    go.textContent = "この内容で入れる";
    go.addEventListener("click", () => sendFiles(false));
    const row = document.createElement("div");
    row.className = "btn-row";
    row.style.marginTop = "var(--sp-3)";
    row.append(go);
    box.append(row);
  }
}

function wireImport() {
  const drop = document.getElementById("import-drop");
  const input = document.getElementById("import-files");

  document.getElementById("import-pick")?.addEventListener(
    "click", () => input?.click());
  input?.addEventListener("change", () => takeFiles(input.files || []));

  if (drop && !drop.dataset.locked) {
    for (const name of ["dragenter", "dragover"]) {
      drop.addEventListener(name, (event) => {
        event.preventDefault();
        drop.dataset.over = "1";
      });
    }
    for (const name of ["dragleave", "drop"]) {
      drop.addEventListener(name, () => { delete drop.dataset.over; });
    }
    drop.addEventListener("drop", (event) => {
      event.preventDefault();
      takeFiles(event.dataTransfer?.files || []);
    });
  }
  // 画面の他所へ落としたときに、ブラウザがそのファイルを開いてしまうのを
  // 止める ── 打ちかけの日報ごと別のページに変わってしまう
  for (const name of ["dragover", "drop"]) {
    document.addEventListener(name, (event) => {
      if (!event.target.closest?.("#import-drop")) event.preventDefault();
    });
  }

  // 決定。渡し方がどちらでも、押すものは1つ
  document.getElementById("import-go")?.addEventListener("click", () => {
    if (picked.length) { sendFiles(false); return; }
    // 道を打って渡したときは、下見を挟んでから入れる
    if (importPath()) applyImport();
  });
  // 道を打ったときも押せるようにする(渡し方は2通りある)
  document.getElementById("import-path")?.addEventListener("input", showPicked);
  showPicked();

  document.getElementById("import-preview")?.addEventListener(
    "click", async () => {
      importNote("");
      try {
        const body = await api.post("/api/settings/import/preview",
                                    { path: importPath(), line: importLine() });
        paintPlan(body);
        if (!body.ok) importNote(body.message, "warn");
      } catch (err) {
        paintPlan(null);
        importNote(err.message, "error");
      }
    });

  document.getElementById("import-template")?.addEventListener(
    "click", async () => {
      try {
        const body = await api.post("/api/settings/import/template", {});
        importNote(body.message, "ok");
      } catch (err) { importNote(err.message, "error"); }
    });
}

/* ================================================================
   参照パス

   **判断はサーバが持つ。** 「変えるのに管理者パスワードが要るか」も
   「その道が読めるか」も、ここでは決めない ── 断られたら理由を出し、
   通ったら返ってきた状態をそのまま描き直す。
   ================================================================ */

/* --- フォルダを辿る -------------------------------------------- */
// ブラウザのファイル選択ダイアログは**クライアント側**の道しか返さない
// ので、サーバ(=このPC)から見たフォルダは選べない。代わりに一覧を引く
let browsingFor = "";
let browsingAt = "";

async function browseTo(path) {
  const body = await api.get(`/api/fs/list?path=${encodeURIComponent(path || "")}`);
  browsingAt = body.path;
  document.getElementById("browse-path").textContent = body.path;

  /*
    **どのドライブの、どこを開いているか。**

    一段ずつ上へ辿るだけでは、共有(Z:)へ移りたいのに行き着けません。
    ドライブを頭に並べておけば1回で移れます。その下の道しるべは
    押せるので、途中の階層へも1回で戻れます。
  */
  const where = document.getElementById("browse-where");
  if (where) {
    where.replaceChildren();
    for (const drive of body.drives || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "browse__drive";
      btn.textContent = drive.name;
      if (body.path.startsWith(drive.path)) btn.dataset.here = "1";
      btn.addEventListener("click", () => browseTo(drive.path).catch(toastError));
      where.append(btn);
    }
    const crumbs = document.createElement("span");
    crumbs.className = "browse__crumbs";
    for (const crumb of body.crumbs || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "browse__crumb";
      btn.textContent = crumb.name;
      btn.addEventListener("click", () => browseTo(crumb.path).catch(toastError));
      crumbs.append(btn);
    }
    where.append(crumbs);
  }

  const host = document.getElementById("browse-body");
  host.textContent = "";
  if (body.error) {
    const msg = document.createElement("div");
    msg.className = "msg msg--warn";
    msg.textContent = body.error;
    host.append(msg);
  }
  if (body.parent) host.append(entry("..", body.parent, "dir"));
  for (const name of body.dirs) {
    host.append(entry(name, `${body.path}/${name}`, "dir"));
  }
  for (const file of body.files) {
    // ファイルは**見せるだけ**。選ぶのはフォルダなので押せなくてよいが、
    // 「このフォルダで合っている」の手がかりになる
    const row = document.createElement("div");
    row.className = "browse__file";
    row.textContent = `${file.name} (${file.size.toLocaleString()} バイト)`;
    host.append(row);
  }
  if (body.truncated) {
    const more = document.createElement("div");
    more.className = "lead";
    more.textContent = "…多すぎるので途中まで出しています";
    host.append(more);
  }
}

function entry(label, path, kind) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "browse__entry";
  btn.dataset.kind = kind;
  btn.textContent = label;
  btn.addEventListener("click", () => browseTo(path).catch(toastError));
  return btn;
}


/* ================================================================
   管理者パスワードを変える
   ================================================================ */
function pwNote(text, kind = "info") {
  const box = document.getElementById("pw-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

function clearPw() {
  for (const id of ["pw-current", "pw-new", "pw-confirm"]) {
    const el = document.getElementById(id);
    if (el) el.value = "";
  }
}



/* ================================================================
   音

   **鳴らすのはブラウザ、決めるのはサーバ。** ここは設定の保存と試聴だけ。
   ================================================================ */
function soundNote(text, kind = "info") {
  const box = document.getElementById("sound-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

/* ================================================================
   配線

   **画面へ来るたびに繋ぎ直す。** ES モジュールは一度しか読まれないので、
   `nav.js` が差し替えで作り直した要素には、前回付けた listener が残って
   いない ── ここを module のトップレベルに書いていたせいで、
   「2 度目に設定を開くとボタンが全部死ぬ」状態になっていた。
   ================================================================ */
/* ================================================================
   いま動いているのはどの版か

   **版だけでは「入れ替えたのに古いまま」を見分けられない。** HTML は
   毎回サーバが作り直すのでいつでも新しく見えますが、JS はブラウザが
   控えているかもしれません。

   サーバが配っている印(`window.APP.stamp`)と、いま動いている画面の
   印(`window.APP.runningStamp`。`app.js` が自分の URL から拾う)を
   並べて出します。**2つが同じなら、画面もサーバも同じもの**です。
   ================================================================ */
function paintStamps() {
  const box = document.getElementById("client-stamp");
  const verdict = document.getElementById("stamp-verdict");
  if (!box || !verdict) return;

  const served = (window.APP && window.APP.stamp) || "";
  const running = (window.APP && window.APP.runningStamp) || "";
  box.textContent = running || "(取れません)";

  if (!running) {
    // 印が取れない場面もある。**分からないことを「古い」と言わない**
    verdict.textContent = "確かめられません";
    verdict.className = "badge";
    return;
  }
  if (running === served) {
    verdict.textContent = "一致";
    verdict.className = "badge badge--done";
    return;
  }
  verdict.textContent = "食い違い ── Ctrl+Shift+R で読み直してください";
  verdict.className = "badge badge--alert";
}

export function start() {
  paintStamps();
  // 面(タブ)。並びも既定もサーバが決めたものを写すだけ
  attachAll();

  /*
    別の画面から「この面を開いて」と連れてこられたとき (`?tab=terminal`)。

    日報入力の「見るだけです」の帯から**管理者モードにする**ところまで
    連れて行くのに要ります ── 設定画面を開いただけでは、6つある面の
    どれに管理者モードがあるのかを探すことになります。
  */
  const wanted = new URLSearchParams(location.search).get("tab");
  if (wanted) {
    document.querySelector(
      `#settingsTabs > .tabs__bar > .tab[data-key="${CSS.escape(wanted)}"]`)
      ?.click();
  }

  // 別の面への案内。パスは「参照設定」の面だけで決めるので、使う側の
  // 面からは**押せば飛べる**ようにする ── 「あちらで決めます」とだけ
  // 書いて自分で探させるのは、探す手間を増やしただけ
  for (const link of document.querySelectorAll("[data-goto-tab]")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const key = link.dataset.gotoTab;
      document.querySelector(
        `#settingsTabs > .tabs__bar > .tab[data-key="${key}"]`)?.click();
      document.querySelector("main#main")?.scrollTo({ top: 0 });
    });
  }

  /* ---- 鍵の帯 ----------------------------------------------------
     **鍵は管理者モードそのもの。** このツールに合言葉は1つしかなく、
     2つ目を作ると現場は両方を紙に貼ります。帯は面ごとに出ますが、
     開けるのはどれも同じ1つの鍵です。

     開いたら画面を描き直します ── 「開きました」とだけ言われても、
     どのボタンが押せるようになったのかは見えません。 */
  for (const btn of document.querySelectorAll("[data-lock-on]")) {
    btn.addEventListener("click", async () => {
      const bar = btn.closest(".lockbar");
      const pw = bar?.querySelector("[data-lock-pw]");
      try {
        const body = await api.post("/api/settings/admin", {
          enable: true, password: pw?.value || "",
        });
        // **合言葉は画面に残さない。** 次に誰かが座ったときそのまま使える
        if (pw) pw.value = "";
        toast(body.message, "ok");
        refresh();
      } catch (err) { toastError(err); }
    });
  }

  for (const btn of document.querySelectorAll("[data-lock-off]")) {
    btn.addEventListener("click", async () => {
      try {
        const body = await api.post("/api/settings/admin", { enable: false });
        toast(body.message, "ok");
        refresh();
      } catch (err) { toastError(err); }
    });
  }

  document.getElementById("back")?.addEventListener("click", async () => {
    try {
      // この画面には明細の値が無いので、何も送らない。DBに入っている
      // ものがそのまま残る。**直した値ごと戻したいときは入力画面の
      // 「最新のページに戻る」**を使う(あちらは値を添えて送る)
      const body = await api.post("/api/settings/back", {});
      toast(body.message, "ok");
      location.href = body.next || "/";
    } catch (err) { toastError(err); }
  });

  // ---- この端末のライン ----
  //
  // **据え付けのときに1度決めるもの。** 日報入力からは触れません
  // (`app/routes/entry.change_line` の説明)。押し間違えると保存先の
  // キーごと変わるので、確かめてから送ります。
  function lineNote(text, kind = "info") {
    const box = document.getElementById("line-note");
    if (!box) return;
    box.hidden = !text;
    box.textContent = text;
    box.className = `msg msg--${kind}`;
  }

  async function setLine(line, maruSub) {
    try {
      const body = await api.post("/api/entry/line",
                                  maruSub ? { line, maru_sub: maruSub } : { line });
      // 覚えられなかったときは**黙って ok にしない** ── 次に起動すると戻る
      toast(body.message || `${lineLabel(line)} にしました`,
            body.remembered === false ? "warn" : "ok");
      refresh();
    } catch (err) { lineNote(err.message, "error"); }
  }

  for (const btn of document.querySelectorAll("#line-choices [data-line]")) {
    btn.addEventListener("click", () => {
      const line = btn.dataset.line;
      if (!confirm(`この端末のラインを ${btn.dataset.label || line} にします。\n` +
                   "打ちかけがあると、別のラインの日報として保存されます。" +
                   "よろしいですか?")) return;
      setLine(line);
    });
  }
  for (const btn of document.querySelectorAll("#maru-subs [data-maru]")) {
    btn.addEventListener("click", () => setLine(btn.dataset.line, btn.dataset.maru));
  }

  // ---- アクセス権限の表(v4.12.0)── 読み直して、ラインが変わっていれば当てる ----
  document.getElementById("access-reload")?.addEventListener("click", async () => {
    const box = document.getElementById("access-note");
    try {
      const body = await api.post("/api/settings/access-rights", {});
      toast(body.message, body.problem ? "warn" : "ok");
      // 当てたらラインの欄ごと描き直す(いまのライン・ボタンの色)
      if (body.applied) refresh();
      else if (box) {
        box.hidden = !body.problem;
        box.textContent = body.problem || "";
        box.className = "msg msg--warn";
      }
    } catch (err) {
      if (box) {
        box.hidden = false;
        box.textContent = err.message;
        box.className = "msg msg--error";
      }
    }
  });

  // ---- 配布設定(`nippou/distribution.py`、python-web-tools と同じ) ----
  //
  // 選んだ項目とパスワードを送り、返ってきた状態で描き直すだけ。
  // **パスワードの値は画面に残さない。** 困りごと(このまま配ると…)は
  // サーバが言う
  function distNote(text, kind = "info") {
    const box = document.getElementById("dist-why");
    if (!box) return;
    box.hidden = !text;
    box.textContent = text;
    box.className = `msg msg--${kind}`;
  }

  async function sendDist(path, body) {
    const pw = document.getElementById("dist-password");
    distNote("");
    try {
      const reply = await api.post(path, { ...body, password: pw?.value || "" });
      if (pw) pw.value = "";
      const alerts = (reply.distribution?.warnings || [])
        .filter((w) => w.level === "alert");
      toast(reply.message || "済みました", alerts.length ? "warn" : "ok");
      refresh();
    } catch (err) {
      distNote(err.message, "error");
    }
  }

  const checkedDist = (attr) => [...document.querySelectorAll(`[${attr}]`)]
    .filter((box) => box.checked)
    .map((box) => box.getAttribute(attr));

  document.getElementById("dist-export")?.addEventListener("click", () => sendDist(
    "/api/settings/distribution/export",
    { items: checkedDist("data-dist-item"), files: checkedDist("data-dist-file") }));
  document.getElementById("dist-reapply")?.addEventListener("click", () => {
    if (!confirm("配布設定を読み込み直します。\n" +
                 "この端末にすでにある設定も、配布設定の値で上書きします。よろしいですか?")) return;
    sendDist("/api/settings/distribution/reapply", {});
  });
  document.getElementById("dist-remove")?.addEventListener("click", () => {
    if (!confirm("配布設定のフォルダを消します(この端末の設定はそのままです)。\n" +
                 "よろしいですか?")) return;
    sendDist("/api/settings/distribution/remove", {});
  });

  document.getElementById("restore-shift")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/settings/restore-shift", {});
      // 戻したぶんも「共有へ未送信」に積まれる。数を描き直してもらう
      toast(body.message, "ok");
      refresh();
    } catch (err) {
      dbNote(err.message, "error");
    }
  });

  wireImport();

  /*
    共有への保存。**断られたら、どの直のどこが引っかかったのかを出す。**

    サーバは 422 と一緒に `reports` を返す(`services/shift_check`)。
    トーストに畳むと、直すたびに押し直して読み返すことになるので、
    ボタンの下に並べたままにする。
  */
  function showCheck(body) {
    const box = document.getElementById("push-check");
    const head = document.getElementById("push-check-head");
    const list = document.getElementById("push-check-list");
    const skipBox = document.getElementById("push-check-skip");
    if (!box || !head || !list) return;

    const reports = (body && body.reports) || [];
    const bad = reports.filter((r) => !r.ok);
    if (!bad.length) { box.hidden = true; return; }

    head.textContent = (body.error && body.error.message)
      ? body.error.message.split("\n")[0] : "保存できません";
    list.replaceChildren();
    let skippable = false;
    for (const report of bad) {
      const group = document.createElement("li");
      const title = document.createElement("b");
      title.textContent = `${report.report_date} ${lineLabel(report.line)} ${report.shift}`;
      group.append(title);
      const inner = document.createElement("ul");
      for (const f of report.findings) {
        if (f.skippable) skippable = true;
        const li = document.createElement("li");
        li.textContent = (f.where ? `${f.where} ` : "") + f.message;
        if (f.how) {
          const how = document.createElement("span");
          how.className = "lead";
          how.textContent = ` — ${f.how}`;
          li.append(how);
        }
        inner.append(li);
      }
      group.append(inner);
      list.append(group);
    }
    if (skipBox) skipBox.hidden = !skippable;
    const phrase = document.getElementById("skip-phrase-text");
    if (phrase && body.skip_phrase) phrase.textContent = body.skip_phrase;
    box.hidden = false;
  }

  async function pushToShared(skip) {
    // **進み具合を出す**(確かめる → 送る → 写す → 月替わり)。`progress.js`
    const stop = watchJob("共有へ保存しています");
    try {
      const body = await api.post("/api/settings/push", skip ? { skip } : {});
      document.getElementById("push-check").hidden = true;
      // **結果はトーストへ。** このあと画面を塗り直すので、本文の中に
      // 書くと一緒に消えます(トーストは外枠なので残ります)
      toast(body.message, body.failed ? "warn" : "ok");
      // 月替わりが一緒に走ったら、その結果も知らせる
      if (body.rollover) toast(body.rollover.message, "ok");
      // **塗り直す。**
      //
      // 「共有へ保存: 1件」と出したすぐ横で、この画面の
      // 「共有へ未送信 1直ぶん」が減らないままでした ── 送り終えた
      // のに送っていないと書いてあるので、開き直すまで信じられません。
      // 数はサーバが描いているので、描き直してもらいます
      refresh();
    } catch (err) {
      if (err.status === 422 && err.body && err.body.reports) {
        showCheck(err.body);
        note("保存できません。下に出ているところを直してください", "error");
        return;
      }
      note(err.message, "error");
    } finally {
      stop();                 // **必ず消す**(枠が出たままになる)
    }
  }

  document.getElementById("push")?.addEventListener("click", () => pushToShared(""));
  document.getElementById("push-skip")?.addEventListener("click", () => {
    const text = (document.getElementById("skip-phrase")?.value || "").trim();
    if (!text) {
      note("通すには、上に書いてある一文をそのまま打ってください", "error");
      return;
    }
    pushToShared(text);
  });

  // ---- 月替わり ----
  function rollNote(text, kind = "info") {
    const box = document.getElementById("rollover-note");
    if (!box) return;
    box.hidden = !text;
    box.textContent = text;
    box.className = `msg msg--${kind}`;
  }

  async function rolloverState() {
    const label = document.getElementById("rollover-state");
    if (!label) return;
    try {
      const body = await api.get("/api/settings/rollover");
      label.textContent = body.due
        ? `月替わりです: ${body.label} を片付けられます`
        : body.reason;
      const button = document.getElementById("rollover");
      if (button) button.classList.toggle("btn--primary", !!body.due);
    } catch (err) {
      label.textContent = err.message;
    }
  }

  document.getElementById("rollover")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/settings/rollover", {});
      rollNote(body.message, body.ran ? "ok" : "info");
      rolloverState();
    } catch (err) {
      rollNote(err.message, "error");
    }
  });

  // 月を入れれば過ぎた月の写しを取り直せる。空なら自動で見つける
  document.getElementById("rollover-export")?.addEventListener("click", async () => {
    const month = document.getElementById("rollover-month")?.value.trim() || "";
    try {
      const body = await api.post("/api/settings/rollover", month ? { month } : {});
      rollNote(body.message, body.ran ? "ok" : "info");
    } catch (err) {
      rollNote(err.message, "error");
    }
  });

  rolloverState();

  // ---- 集計の作り直し ----
  const rebuildStart = document.getElementById("rebuild-start");
  const rebuildEnd = document.getElementById("rebuild-end");
  // 既定は今月の1日〜今日。**いちばん押されるのがこの範囲**
  if (rebuildStart && rebuildEnd && !rebuildStart.value) {
    const now = new Date();
    const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1)
      .padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    rebuildStart.value = iso(new Date(now.getFullYear(), now.getMonth(), 1));
    rebuildEnd.value = iso(now);
  }

  document.getElementById("rebuild-summary")?.addEventListener("click", async () => {
    const box = document.getElementById("rebuild-note");
    const show = (text, kind) => {
      if (!box) return;
      box.hidden = !text;
      box.textContent = text;
      box.className = `msg msg--${kind}`;
    };
    try {
      const body = await api.post("/api/settings/rebuild-summary", {
        start: rebuildStart?.value || "",
        end: rebuildEnd?.value || "",
      });
      show(body.message, body.shifts ? "ok" : "info");
    } catch (err) {
      show(err.message, "error");
    }
  });

  /* ================================================================
     中身の無い紙を片付ける

         残っているのか見えなければ消せないですよね

     数が出るだけでは消せません。どの日の・どの直の・どのページなのかを
     並べて、そこから1枚ずつ消せるようにします。

     **消してよいかを決めるのはサーバ**(`services/empty_pages.py`)。
     ここは並べて、押されたら送るだけです ── 一覧を出してから押すまでの
     あいだに誰かが打っているかもしれないので、画面の言うことは当てに
     しません。
     ================================================================ */
  const emptyNote = (text, kind) => {
    const box = document.getElementById("empty-pages-note");
    if (!box) return;
    box.hidden = !text;
    box.textContent = text || "";
    box.className = `msg msg--${kind}`;
  };

  function paintEmptyPages(body) {
    const head = document.getElementById("empty-pages-head");
    if (head) head.textContent = body.headline || "";
    const box = document.getElementById("empty-pages-box");
    const rows = document.getElementById("empty-pages-rows");
    if (!rows || !box) return;
    rows.replaceChildren();
    box.hidden = !(body.pages || []).length;
    for (const p of body.pages || []) {
      const tr = document.createElement("tr");
      for (const text of [p.report_date, p.line, p.shift, `第${p.page}ページ`,
                          p.worker || "(空)", p.saved_at || ""]) {
        const td = document.createElement("td");
        td.textContent = text;
        tr.append(td);
      }
      const last = document.createElement("td");
      if (p.can_delete) {
        const btn = document.createElement("button");
        btn.className = "btn btn--sm";
        btn.type = "button";
        btn.textContent = "消す";
        btn.addEventListener("click", () => removeEmptyPage(p));
        last.append(btn);
      } else {
        // **消せない理由をその行に書く。** ボタンだけ消すと、なぜ
        // 押せないのかが分かりません
        const why = document.createElement("span");
        why.className = "lead";
        why.textContent = p.note || "";
        last.append(why);
      }
      tr.append(last);
      rows.append(tr);
    }
  }

  async function findEmptyPages(quiet = false) {
    try {
      const body = await api.get("/api/settings/empty-pages");
      paintEmptyPages(body);
      if (!quiet) emptyNote(body.note || "", "info");
      return body;
    } catch (err) {
      emptyNote(err.message, "error");
      return null;
    }
  }

  async function removeEmptyPage(page) {
    // **取り返しがつかないので確かめる。**
    if (!confirm(`${page.key_text} を消します。よろしいですか?`)) return;
    try {
      const body = await api.post("/api/settings/empty-pages/delete", {
        report_date: page.report_date, line: page.line,
        shift: page.shift, page: page.page,
      });
      paintEmptyPages(body);
      emptyNote(body.message, "ok");
    } catch (err) {
      // 断られた(打ってある / 共有へ渡してある)。**一覧を出し直す**
      emptyNote(err.message, "warn");
      await findEmptyPages(true);
    }
  }

  document.getElementById("empty-pages-find")
    ?.addEventListener("click", () => findEmptyPages());

  /* ---- ライン名のコンバート(v4.13.0)----------------------------------
     数はサーバが数えます(`services/line_rename`)。ここは表に並べるだけ。 */
  function renameNote(text, kind = "info") {
    const box = document.getElementById("line-rename-note");
    if (!box) return;
    box.hidden = !text;
    box.textContent = text || "";
    box.className = `msg msg--${kind}`;
  }

  function paintRename(body) {
    const rows = document.getElementById("line-rename-rows");
    const box = document.getElementById("line-rename-box");
    const head = document.getElementById("line-rename-head");
    if (head) head.textContent = body.message || "";
    if (!rows || !box) return;
    rows.replaceChildren();
    for (const line of body.lines || []) {
      const tr = document.createElement("tr");
      const cells = [lineLabel(line.official), line.found ? line.old_pages : "前の表なし",
                     ...(body.kinds || []).map((kind) => line.counts?.[kind] ?? 0),
                     line.error || line.checked || "—"];
      for (const value of cells) {
        const td = document.createElement("td");
        td.textContent = String(value);
        tr.append(td);
      }
      if (line.error) tr.className = "is-error";
      rows.append(tr);
    }
    box.hidden = !(body.lines || []).length;
    const notes = [];
    for (const line of body.lines || []) {
      for (const note of line.notes || []) notes.push(`${lineLabel(line.official)}: ${note}`);
    }
    if ((body.unknown_tables || []).length) {
      notes.push(`前でも正規でもない名前の表(写していません): ${body.unknown_tables.join("・")}`);
    }
    if (body.error) renameNote(body.error, "error");
    else renameNote(notes.join("\n"), notes.length ? "warn" : "info");
  }

  document.getElementById("line-rename-survey")?.addEventListener("click", async () => {
    try {
      paintRename(await api.get("/api/settings/line-rename"));
    } catch (err) {
      renameNote(err.message, "error");
    }
  });

  document.getElementById("line-rename-run")?.addEventListener("click", async () => {
    if (!confirm("共有の日報データへコンバートします(前の表には触りません)。\n" +
                 "よろしいですか?")) return;
    try {
      const body = await api.post("/api/settings/line-rename", {});
      paintRename(body);
      toast(body.message, body.ok ? "ok" : "warn");
    } catch (err) {
      renameNote(err.message, "error");
    }
  });

  document.getElementById("sync-shift")?.addEventListener("click", async () => {
    const box = document.getElementById("shift-note");
    const say = (text, kind) => {
      if (!box) return;
      box.hidden = !text;
      box.textContent = text;
      box.className = `msg msg--${kind}`;
    };
    try {
      const body = await api.post("/api/settings/sync-shift", {});
      say(body.message, "ok");
      // 取り込んだ時刻をその場で見せる。**「入った」だけでは確かめられない**
      refresh();
    } catch (err) {
      say(err.message, "error");
    }
  });

  /* ---- 置き場所(参照パス)の保存 ----------------------------------
     **欄ごとに保存します。** まとめて1つのボタンだと、どこまでが
     保存の対象なのかが読めず、押すたびに他の欄まで巻き込みます。

     打っただけでは効いていないのに見た目が変わらないと、「もう保存
     された」と読めてしまいます ── 打ちかけは「未保存」、保存できたら
     「保存しました」を欄の見出しに出し、実際に見る道もその場で書き
     換えます(画面ごと描き直さないので、どこを触ったか見失いません)。 */
  function rowOf(el) { return el.closest(".path"); }

  function markState(row, state, text) {
    if (!row) return;
    if (state) row.dataset.pathState = state;
    else delete row.dataset.pathState;
    const label = row.querySelector("[data-path-state]");
    if (label) label.textContent = text || "";
  }

  function groupNote(group, text, kind = "info") {
    const box = group?.querySelector("[data-path-note]");
    if (!box) return;
    box.hidden = !text;
    box.textContent = text;
    box.className = `msg msg--${kind}`;
  }

  /**
   * 保存できたぶんを、返ってきた状態で描き直す。**取り直しに行かない。**
   *
   * ぶら下がっているファイルの道も一緒に直します ── 親の道だけ新しく
   * なって、その下の一覧が古いままだと、**どちらが本当か分かりません。**
   */
  function repaintFiles(row, key, files) {
    const list = row.querySelectorAll(".path__file");
    const kids = (files || []).filter((f) => f.source_key === key);
    list.forEach((li, index) => {
      const kid = kids[index];
      if (!kid) return;
      const code = li.querySelector(".path__file-path");
      if (code) code.textContent = kid.path;
      const badge = li.querySelector(".badge + .badge") || null;
      if (!badge) return;
      if (kid.is_write_target && !kid.exists && kid.writable) {
        badge.className = "badge";
        badge.textContent = "初回の保存で作られます";
      } else if (!kid.exists) {
        badge.className = "badge badge--alert";
        badge.textContent = "このファイルがありません";
      } else if (kid.readable) {
        badge.className = "badge badge--done";
        badge.textContent = "読めます";
      } else {
        badge.className = "badge badge--alert";
        badge.textContent = kid.error;
      }
    });
  }

  function repaintPath(row, view) {
    if (!row || !view) return;
    const now = row.querySelector(".path__now");
    if (!now) return;
    const code = now.querySelector("code");
    if (code) code.textContent = view.resolved;
    // 「既定 / 相対 / あります・ありません」の札を作り直す
    for (const badge of now.querySelectorAll(".badge")) badge.remove();
    const marks = [];
    if (view.off) {
      // 空なら出さない欄を空にした(`OFF_WHEN_EMPTY_KEYS`)。道も探さない
      marks.push(["badge", "使っていません"]);
    } else {
      if (view.from_site) marks.push(["badge", "配布設定の値"]);
      else if (view.is_default) marks.push(["badge", "既定"]);
      if (view.is_relative) marks.push(["badge badge--todo", "相対(アプリのフォルダから)"]);
      marks.push(view.exists
        ? ["badge badge--done", view.found_text]
        : ["badge badge--alert", view.missing_text]);
    }
    const before = now.querySelector("button");
    for (const [cls, text] of marks) {
      const span = document.createElement("span");
      span.className = cls;
      span.textContent = text;
      now.insertBefore(span, before);
    }
  }

  for (const input of document.querySelectorAll("[data-path-key] input[type=text]")) {
    input.addEventListener("input", () =>
      markState(rowOf(input), "dirty", "未保存"));
  }

  for (const btn of document.querySelectorAll("[data-save-path]")) {
    btn.addEventListener("click", async () => {
      const row = rowOf(btn);
      const group = btn.closest("[data-path-group]");
      const key = row?.dataset.pathKey;
      if (!key) return;
      const payload = {};
      payload[key] = document.getElementById(`path-${key}`)?.value ?? "";
      const pw = btn.closest(".tabpanel")?.querySelector("[data-lock-pw]");
      if (pw?.value) payload.password = pw.value;

      try {
        const body = await api.post("/api/settings/paths", payload);
        // **合言葉は画面に残さない。** 次に誰かが座ったときそのまま使える
        if (pw) pw.value = "";
        groupNote(group, "");
        markState(row, "saved", "保存しました");
        repaintPath(row, (body.paths || []).find((v) => v.key === key));
        repaintFiles(row, key, body.files);
        toast(body.message, "ok");
      } catch (err) {
        // 403(パスワードが要る)も同じ扱い。**文言はサーバのものをそのまま**
        markState(row, "dirty", "保存できませんでした");
        groupNote(group, err.message, "error");
      }
    });
  }

  /* ---- 読み直す ---------------------------------------------------
     **最初に失敗したままにしない。** 置き場所を直しても、見に行った
     結果(あります / ありません)はその時のものです。共有が後から
     繋がった・ファイルを置いた、のときに確かめる先が要ります。

     手元の写しも捨ててから読み直します ── 捨てないと、繋がる前に
     作った古い写しをそのまま読みます。 */
  document.getElementById("paths-recheck")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/settings/paths/recheck", {});
      toast(body.message, body.problems?.length ? "warn" : "ok");
      refresh();
    } catch (err) { toastError(err); }
  });

  /* ---- フォルダを選ぶ窓 ------------------------------------------
     窓は画面に1つだけ持ち、**押した欄の真下へ移して**出します ──
     面ごとに窓を用意すると同じものが5つになり、画面の下に固定すると
     「どの欄のことを選んでいるのか」が分からなくなります。 */
  for (const btn of document.querySelectorAll("[data-browse]")) {
    btn.addEventListener("click", async () => {
      browsingFor = btn.dataset.browse;
      const current = document.getElementById(`path-${browsingFor}`).value;
      // デスクトップ版は OS の標準の「フォルダの選択」(共有フォルダも辿れる)。
      // 開けなかったときだけ、いつもの一覧へ
      if (desktop.isDesktop) {
        try {
          const picked = await desktop.pickPath("folder", { start: current, title: "フォルダを選ぶ" });
          if (picked) putPicked(browsingFor, picked);
          return;
        } catch (err) {
          console.warn("フォルダの選択窓を開けませんでした。一覧で選びます", err);
        }
      }
      const box = document.getElementById("browse");
      const row = btn.closest(".path");
      if (box && row) row.append(box);
      if (box) box.hidden = false;
      try {
        await browseTo(current);
      } catch (err) { toastError(err); }
    });
  }

  document.getElementById("browse-close")?.addEventListener("click", () => {
    document.getElementById("browse").hidden = true;
  });

  document.getElementById("browse-pick")?.addEventListener("click", () => {
    if (!browsingFor) return;
    const box = document.getElementById("browse");
    if (box) box.hidden = true;
    putPicked(browsingFor, browsingAt);
  });

  /* 選んだフォルダを欄に入れる(一覧からでも、OS の選択窓からでも同じ)。
     **選んだだけでは保存しない。** 欄に入れて、保存ボタンを押させる ──
     置き場所の変更にはパスワードが要るので、ここで確定させると
     「見に行っただけなのに聞かれる」ことになる */
  function putPicked(key, path) {
    const field = document.getElementById(`path-${key}`);
    if (field) field.value = path;
    const row = field?.closest(".path");
    if (row) {
      row.dataset.pathState = "dirty";
      const label = row.querySelector("[data-path-state]");
      if (label) label.textContent = "未保存";
    }
    groupNote(field?.closest("[data-path-group]"),
              "欄に入れました。隣の「保存」を押すと反映します。", "info");
  }

  document.getElementById("pw-change")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/settings/admin-password", {
        current: document.getElementById("pw-current").value,
        new: document.getElementById("pw-new").value,
        confirm: document.getElementById("pw-confirm").value,
      });
      clearPw();
      pwNote(body.message, "ok");
    } catch (err) {
      // **入れた値は消さない。** 打ち間違いのときに全部打ち直させない
      pwNote(err.message, "error");
    }
  });

  document.getElementById("pw-reset")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/settings/admin-password", {
        reset: true, current: document.getElementById("pw-current").value,
      });
      clearPw();
      pwNote(body.message, "ok");
    } catch (err) {
      pwNote(err.message, "error");
    }
  });

  document.getElementById("save-sounds")?.addEventListener("click", async () => {
    const payload = {};
    for (const el of document.querySelectorAll("[data-sound-file]")) {
      payload[el.dataset.soundFile] = el.value;
    }
    try {
      const body = await api.post("/api/settings/paths", payload);
      // 文言はトーストで(描き直すと画面の中の文言は消える)
      toast(body.message || "音の設定を保存しました", "ok");
      // **鳴らしてよい出来事を読み直す** ── 1分待たずに、いまの選び方で鳴る
      loadCues();
      // 保存できたら描き直す。「鳴らします/ありません」まで見せたい
      refresh();
    } catch (err) {
      soundNote(err.message, "error");
    }
  });

  for (const btn of document.querySelectorAll("[data-preview]")) {
    btn.addEventListener("click", async () => {
      // **選んであるもの(保存前でも)を鳴らす。** 押した直後なので必ず鳴らせる
      // (自動再生の制限にかからない)
      const picked = document.getElementById(`sound-${btn.dataset.preview}`);
      const name = picked ? picked.value : "";
      if (!name || name === picked.querySelector("option")?.value) {
        soundNote("「鳴らさない」を選んであります", "info");
        return;
      }
      const ok = await previewFile(name);
      soundNote(ok ? `鳴らしました: ${name}`
                   : `鳴らせませんでした: ${name}(フォルダに無いか、形式が読めません)`,
                ok ? "ok" : "warn");
    });
  }

  // ---- ライン毎目標(45度線) ----
  //
  // **上書きなので、押す前に一度止める。** 共有に置いた1つのファイルを
  // 書き換えるので、いま手で直した目標が消える。押した人には
  // 「取り込むだけ」に見えるところなので、消える側を先に言う
  // **いちばん早い道。** 全ラインぶんの行を書き出して、書き換えてもらう
  //
  // 見本 → 書き換える → `wireTargetFile()` で渡す → 効く、が一周。
  // 渡す口が無かったので、見本を出しても本物にする道がありませんでした
  document.getElementById("line-target-template")
    ?.addEventListener("click", async () => {
      const box = document.getElementById("line-target-note");
      const say = (text, kind = "info") => {
        if (!box) return;
        box.hidden = !text;
        box.textContent = text;
        box.className = `msg msg--${kind}`;
      };
      try {
        const body = await api.post("/api/settings/line-targets/template", {});
        say(body.message, "ok");
        toast("見本を出しました", "ok");
      } catch (err) { say(err.message, "error"); }
    });

  wireTargetFile();
  wireTargetReload();
  wireStopReasons();

  document.getElementById("import-line-targets")
    ?.addEventListener("click", async () => {
      const box = document.getElementById("line-target-note");
      const say = (text, kind = "info") => {
        if (!box) return;
        box.hidden = !text;
        box.textContent = text;
        box.className = `msg msg--${kind}`;
      };
      if (!confirm("マスタの値で目標CSVを作り直します。\n" +
                   "いまCSVに入っている目標は上書きされます。よろしいですか?")) {
        say("取り込みをやめました。CSVはそのままです。", "info");
        return;
      }
      try {
        const body = await api.post("/api/settings/line-targets/import", {});
        toast(body.message, "ok");
        // 取り込んだ値をその場で見せる。**「入った」だけでは確かめられない**
        refresh();
      } catch (err) {
        say(err.message, "error");
      }
    });
}
