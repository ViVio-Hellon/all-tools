/*
  views/vc.js — VC長さ計算(vc-calculator の calc.js / quick.js / coil.js の移植)

  面は3つ(並びはサーバ `presenters/vc.TABS`):
    計算          品種 → 内径 → 肉厚 → 計算。VC長さと枚数、図と計算の経過
    早見表        肉厚 → 長さの表。A4 横で印刷。管理者はマスをまとめて作れる
    コイル・平板  別に作られたツールを枠(iframe)で載せる。開いたときに読み込む

  【判断はサーバ】品種を選ぶ(List選択)・内径を選ぶ(Big/Small)・計算する
  (CommandButton1_Click)の3つとも、いまのマスタを読んだサーバが欄を作り直して
  返す。ここは欄の文字を送り、返ってきた欄と結果を描くだけ。

  【この画面だけが持つ状態】どの欄が「計算で入った」か(`computed`)。
  VBA は VC長さが入っていると上書きしないので、肉厚を直して再計算すると
  古い長さが残った(docs/vc/VBA解析.md §14 D1)。計算で入った欄は、元になる欄を
  直した時点で消す。**手で入れた VC長さは消さない**(VBA と同じく、その長さで
  枚数を出す)。

  【`start()` の中で全部やる理由】
  この画面へ2度目に来たとき、モジュールは読み直されない(ES モジュールは
  一度きり)。**要素は毎回引き直す**必要があるので、状態も配線もここに閉じる。
*/
import { api, ApiError, background } from "../api.js";
import { openWindow } from "../desktop.js";
import { onLeave, pageSignal } from "../nav.js";
import { attachAll, current, select as selectTab } from "../tabs.js";
import { toast, toastError } from "../toast.js";
import { drawRoll, linkHighlight } from "../vc_roll.js";

const FIELDS = ["coatu", "vcatu", "inside", "vclen", "prolen"];
// ある欄を直したら、どの「計算で入った欄」が古くなるか
const SOURCES = { vclen: ["coatu", "vcatu", "inside"], coatu: ["vclen", "vcatu", "inside"] };
// マスタがほかで直されたかを見に行く間隔(ms)。**打った内容は変えない**
const WATCH_MS = 60000;
// コイル・平板の枠の低いほうの限り(これより低い窓では外のページごと動かす)
const FRAME_MIN = 480;

const $ = (id) => document.getElementById(id);

export function start() {
  const signal = pageSignal();
  const root = $("vcTabs");
  if (!root) return;

  // ---- この画面の状態(画面を出たら捨てる) ----
  let state = null;          // サーバが返した品種・定尺・マスタの状態
  let selected = null;       // 選んでいる品種名
  let chosenInside = null;   // 押した内径の選択肢
  let computed = new Set();  // 計算で入った欄
  let result = null;         // 最後の計算結果
  let revision = null;       // 最後に見たマスタの更新番号
  let quickView = null;      // 最後に描いた早見表

  const input = (name) => $(`vc-${name}`);

  // ==================================================================
  // 面
  // ==================================================================
  root.addEventListener("tab:select", (event) => shown(event.detail.key), { signal });
  attachAll();
  shown(current(root));

  function shown(key) {
    if (key === "quick") loadQuick();
    if (key === "settings") loadSettings();
    if (key === "coil") {
      // **開いたときに読み込む。** 3D とグラフのライブラリは大きいので、
      // 計算しか使わない人に毎回読ませない
      const frame = $("vc-coil-frame");
      if (frame && !frame.getAttribute("src")) frame.setAttribute("src", frame.dataset.src);
      requestAnimationFrame(fitFrame);
    }
  }

  // **コイル・平板の枠は、作業面の残りの高さちょうどにする。** 外のページまで
  // 動くと、枠の中で収めても「少しスクロール」が残る。中で収めるのは枠の中のツール
  // (static/vc/coil/coil-fit.js)。上に帯(マスタの知らせ)が出ても出なくても合う
  function fitFrame() {
    const frame = $("vc-coil-frame");
    const work = frame && frame.closest(".work");
    if (!work || frame.offsetParent === null) return;
    const top = frame.getBoundingClientRect().top - work.getBoundingClientRect().top
      + work.scrollTop;
    const pad = parseFloat(getComputedStyle(work).paddingBottom) || 0;
    const room = Math.floor(work.clientHeight - top - pad);
    frame.style.height = `${Math.max(FRAME_MIN, room)}px`;
  }
  window.addEventListener("resize", fitFrame, { signal });

  // ==================================================================
  // マスタの状態(帯)
  // ==================================================================
  function showMaster(master) {
    const box = $("vc-master-note");
    if (!box || !master) return;
    box.hidden = !master.note;
    box.textContent = master.note || "";
    if (current(root) === "coil") requestAnimationFrame(fitFrame);
    if (revision !== null && master.revision !== revision && master.source === "db") {
      window.dispatchEvent(new CustomEvent("vc:master-changed"));
    }
    revision = master.revision;
  }

  // ほかの端末でマスタが直された(更新番号が変わった)。一覧と早見表を描き直す
  window.addEventListener("vc:master-changed", () => {
    reloadState();
    if (current(root) === "quick") loadQuick();
  }, { signal });
  const watch = setInterval(async () => {
    try {
      const body = await background.get("/api/vc/state");
      if (body.state.master.revision !== revision) applyState(body.state, true);
    } catch (err) { /* 見張りの失敗は黙る(帯は health.js が出す) */ }
  }, WATCH_MS);
  onLeave(() => clearInterval(watch));

  // ==================================================================
  // 計算 ── 読む・描く
  // ==================================================================
  for (const name of FIELDS) {
    input(name).addEventListener("input", () => changed(name), { signal });
  }
  $("vc-form").addEventListener("submit", (event) => { event.preventDefault(); run(); },
                                { signal });
  $("vc-products").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-name]");
    if (button) select(button.dataset.name);
  }, { signal });
  $("vc-inside-choices").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-inside]");
    if (button) chooseInside(button.dataset.inside);
  }, { signal });
  $("vc-open-quick").addEventListener("click", () => {
    // VBA は UFquick をモードレスで開いた。計算画面と並べて見られるよう別の窓にする
    // **早見表だけの窓**(v4.16.0)。帯もレールも無いので、別窓から日報へ戻れない
    // デスクトップ版は外枠の別窓(同じ名前の窓が開いていれば前に出す)
    if (openWindow("/vc?window=quick&tab=quick",
                   { title: "VC 早見表", label: "vc-quick", width: 1280, height: 860 })) return;
    window.open("/vc?window=quick&tab=quick", "vc-quick", "width=1280,height=860");
  }, { signal });
  linkHighlight(() => $("vc-roll")?.querySelector("svg"), $("vc-steps"));
  loadState();

  async function loadState() {
    try {
      const body = await api.get("/api/vc/state");
      applyState(body.state);
    } catch (err) {
      toastError(err);
    }
  }

  /** ほかの端末でマスタが直された。一覧だけ描き直し、打った内容は触らない。 */
  async function reloadState() {
    try {
      const body = await api.get("/api/vc/state");
      applyState(body.state, true);
    } catch (err) {
      toastError(err);
    }
  }

  function applyState(next, fromWatch = false) {
    const before = selected && productOf(selected);
    state = next;
    showMaster(state.master);
    // 肉厚の逆算を使うか(アプリ設定)で、VC長さの欄の説明を変える
    const hint = $("vc-hint-vclen");
    if (hint) {
      hint.textContent = state.reverse
        ? "空欄なら肉厚から計算。入れてあればその長さで枚数を出し、肉厚が空なら肉厚を逆算します。"
        : "空欄なら肉厚から計算。入れてあればその長さで枚数を出します。";
    }
    renderProducts();
    renderInsides();
    renderResult();
    if (!fromWatch || !selected) return;
    const now = productOf(selected);
    if (!now) {
      toast(`選んでいた「${selected}」がマスタから無くなりました。品種を選び直してください。`, "warn");
    } else if (before && (before.vcatu !== now.vcatu || before.inside !== now.inside)) {
      toast(`「${selected}」の VC厚・内径がマスタで変わりました。品種を選び直すと新しい値が入ります。`, "warn");
    }
  }

  function productOf(name) {
    return ((state && state.products) || []).find((p) => p.name === name) || null;
  }

  /** 「2008系/2001SR/310GH5」を / の後ろで折り返せるように入れる。 */
  function breakable(node, text) {
    node.replaceChildren();
    String(text).split("/").forEach((part, i, all) => {
      node.append(part + (i < all.length - 1 ? "/" : ""));
      if (i < all.length - 1) node.append(document.createElement("wbr"));
    });
  }

  function specText(p) {
    const inside = p.chooses_inside
      ? p.choices.map((c) => `${c.label}${c.inside}`).join("・")
      : (p.inside || "未設定");
    return `VC厚 ${p.vcatu} / 内径 ${inside}`;
  }

  function renderProducts() {
    const products = state.products || [];
    $("vc-product-count").textContent = `${products.length} 品種`;
    const box = $("vc-products");
    box.replaceChildren(...products.map((p) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "vc-product";
      b.dataset.name = p.name;
      b.setAttribute("aria-pressed", String(p.name === selected));
      const vendor = document.createElement("span");
      vendor.className = "vc-product__vendor";
      vendor.textContent = p.vendor;
      const name = document.createElement("span");
      name.className = "vc-product__name";
      breakable(name, p.name);
      const spec = document.createElement("span");
      spec.className = "vc-product__spec";
      spec.textContent = specText(p);
      b.append(vendor, name, spec);
      if (p.inside_missing) {
        const warn = document.createElement("span");
        warn.className = "vc-product__warn";
        warn.textContent = "内径がマスタにありません(内径の欄に入れてください)";
        b.append(warn);
      }
      return b;
    }));
    if (!products.length) {
      const p = document.createElement("p");
      p.className = "lead";
      p.textContent = "品種がありません。設定・管理者 → マスタ管理 → VC計算マスタ → VC品種 で足してください。";
      box.replaceChildren(p);
    }
  }

  /** 内径を選ぶ品種のときだけ出す(VBA の Nittou 枠)。 */
  function renderInsides() {
    const p = selected && productOf(selected);
    const show = Boolean(p && p.chooses_inside);
    $("vc-insides").hidden = !show;
    if (!show) return;
    $("vc-inside-choices").replaceChildren(...p.choices.map((c) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn";
      b.dataset.inside = c.inside;
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(chosenInside === c.inside));
      b.textContent = `${c.label}  ${c.inside} mm`;
      return b;
    }));
  }

  function renderResult() {
    const p = selected && productOf(selected);
    $("vc-picked-vendor").textContent = p ? p.vendor : "";
    breakable($("vc-picked-name"), p ? p.name : (selected || "品種を選んでください"));
    $("vc-picked-spec").textContent = p ? specText(p) : "";

    $("vc-out-length").textContent = result && result.fields.vclen ? result.fields.vclen : "—";
    const counts = result ? result.counts : (state.sheets || []).map((s) => ({ ...s, value: "" }));
    // 現場の呼び名(1×2 など)だけを出す。記号(M1)や丈(mm)は出さない(利用者の要望)
    const tiles = counts.map((c) => tile(c.label || c.key, "", c.value));
    const prolen = result ? result.fields.prolen : input("prolen").value.trim();
    tiles.push(tile("経寸丈", prolen ? `${prolen} mm` : "未入力", result ? result.mk : ""));
    $("vc-counts").replaceChildren(...tiles);

    const notes = (result && result.notes) || [];
    const list = $("vc-notes");
    list.hidden = !notes.length;
    list.replaceChildren(...notes.map((n) => {
      const li = document.createElement("li");
      li.textContent = n;
      return li;
    }));
    renderExplain();
  }

  /** 図と計算の経過。サーバが返した段と寸法をそのまま描く。 */
  function renderExplain() {
    const steps = (result && result.steps) || [];
    const geo = result && result.geometry;
    const box = $("vc-explain");
    box.hidden = !(steps.length && geo && geo.outer);
    if (box.hidden) return;
    $("vc-roll").replaceChildren(drawRoll(geo));
    $("vc-steps").replaceChildren(...steps.map((st) => {
      const li = document.createElement("li");
      li.className = "vc-stepline";
      li.tabIndex = 0;
      if (st.part) li.dataset.part = st.part;
      const head = document.createElement("div");
      head.className = "vc-stepline__head";
      const title = document.createElement("b");
      title.textContent = st.title;
      const formula = document.createElement("span");
      formula.className = "vc-stepline__formula";
      formula.textContent = st.formula;
      head.append(title, formula);
      const work = document.createElement("div");
      work.className = "vc-stepline__work";
      const eq = document.createElement("span");
      eq.textContent = `${st.work} = `;
      const val = document.createElement("strong");
      val.textContent = `${st.value} ${st.unit}`;
      work.append(eq, val);
      li.append(head, work);
      if (st.note) {
        const note = document.createElement("small");
        note.className = "vc-stepline__note";
        note.textContent = st.note;
        li.append(note);
      }
      return li;
    }));
  }

  function tile(label, sub, value) {
    const box = document.createElement("div");
    box.className = `vc-count${value ? "" : " vc-count--none"}`;
    const k = document.createElement("span");
    k.className = "vc-count__k";
    k.textContent = sub ? `${label}(${sub})` : label;
    const v = document.createElement("span");
    v.className = "vc-count__v";
    v.textContent = value || "—";
    if (value) {
      const unit = document.createElement("small");
      unit.textContent = "枚";
      v.append(unit);
    }
    box.append(k, v);
    return box;
  }

  function setStale(on, why = "入力が変わりました。「計算」を押すと出し直します。") {
    const box = document.querySelector(".vc-result");
    box.classList.toggle("is-stale", Boolean(on && result));
    const badge = $("vc-result-state");
    badge.hidden = !(on && result);
    badge.textContent = on && result ? "古い結果" : "";
    badge.title = why;
  }

  // ==================================================================
  // 計算 ── 欄
  // ==================================================================
  function fields() {
    const out = {};
    for (const name of FIELDS) out[name] = input(name).value;
    return out;
  }

  function writeFields(values, sent = null) {
    for (const name of FIELDS) {
      if (!values || !(name in values)) continue;
      // 返事を待つあいだに打った欄は、打った内容を残す(返事で上書きしない)
      if (sent && input(name).value !== sent[name]) continue;
      input(name).value = values[name];
    }
    for (const name of FIELDS) input(name).classList.toggle("is-auto", computed.has(name));
  }

  /** 焦点がまだ `area` の中(か、どこにも無い)か。 */
  function stillOn(area) {
    const active = document.activeElement;
    return !active || active === document.body || area.contains(active);
  }

  /** 送ったときから変わった欄があるか(返事を待つあいだに打った)。 */
  function typedSince(sent) {
    return FIELDS.some((name) => input(name).value !== sent[name]);
  }

  function showErrors(errors) {
    for (const name of FIELDS) {
      const box = $(`vc-err-${name}`);
      const message = errors && errors[name];
      input(name).setAttribute("aria-invalid", String(Boolean(message)));
      if (box) {
        box.hidden = !message;
        box.textContent = message || "";
      }
    }
  }

  function currentErrors() {
    const out = {};
    for (const name of FIELDS) {
      const box = $(`vc-err-${name}`);
      if (box && !box.hidden) out[name] = box.textContent;
    }
    return out;
  }

  /** 欄を直した。計算で入った欄のうち、これを元にしていたものを消す(D1)。 */
  function changed(name) {
    computed.delete(name);                      // 手で直した欄は「手で入れた」扱い
    for (const [target, sources] of Object.entries(SOURCES)) {
      if (computed.has(target) && sources.includes(name)) {
        input(target).value = "";
        computed.delete(target);
      }
    }
    if (name === "inside") {
      chosenInside = null;
      renderInsides();
    }
    writeFields(null);
    showErrors({ ...currentErrors(), [name]: "" });
    setStale(true);
  }

  // ==================================================================
  // 計算 ── 操作
  // ==================================================================
  async function select(name) {
    const sent = fields();
    try {
      const body = await api.post("/api/vc/select", { product: name, fields: sent });
      selected = body.product;
      chosenInside = null;
      computed = new Set();
      result = null;                             // 品種を変えたら結果も消す(D2)
      writeFields(body.fields, sent);
      showErrors({});
      $("vc-calc-error").hidden = true;
      applyState(body.state);
      setStale(false);
      // 次の欄へ送るのは、まだ押したところに居るときだけ(返事を待つあいだに
      // 別の欄へ移って打ち始めていたら、その欄から奪わない)
      if (stillOn($("vc-products"))) {
        const p = productOf(selected);
        if (p && p.chooses_inside) $("vc-inside-choices").querySelector("button")?.focus();
        else input("coatu").focus();
      }
    } catch (err) {
      if (err instanceof ApiError && err.body && err.body.state) applyState(err.body.state);
      toastError(err);
    }
  }

  async function chooseInside(inside) {
    const sent = fields();
    try {
      const body = await api.post("/api/vc/inside",
                                  { product: selected, inside, fields: sent });
      // 変わるのは内径の欄だけ(Big_Click / Small_Click)
      const before = input("inside").value;
      writeFields({ inside: body.fields.inside }, sent);
      if (before !== input("inside").value) changed("inside");
      chosenInside = body.choice;
      const here = stillOn($("vc-inside-choices"));
      applyState(body.state);
      if (here) input("coatu").focus();
    } catch (err) {
      if (err instanceof ApiError && err.body && err.body.state) applyState(err.body.state);
      toastError(err);
    }
  }

  async function run() {
    $("vc-calc-error").hidden = true;
    const sent = fields();
    try {
      const body = await api.post("/api/vc/run", { product: selected, fields: sent });
      result = body.result;
      const typed = typedSince(sent);
      computed = new Set(typed ? [] : result.computed);
      writeFields(body.fields, sent);
      showErrors({});
      applyState(body.state);
      setStale(typed);
    } catch (err) {
      if (err instanceof ApiError && err.body && err.body.result) {
        const r = err.body.result;
        writeFields(err.body.fields, sent);
        showErrors(r.errors);
        const box = $("vc-calc-error");
        box.hidden = false;
        box.textContent = r.message;
        if (err.body.state) applyState(err.body.state);
        setStale(true, r.message);
        const first = FIELDS.find((n) => r.errors && r.errors[n]) ||
                      FIELDS.find((n) => ["coatu", "vcatu", "inside"].includes(n)
                                         && !input(n).value);
        if (first) input(first).focus();
        return;
      }
      toastError(err);
    }
  }

  // ==================================================================
  // 早見表(VBA `UFquick`)
  // ==================================================================
  const blocksEl = $("vc-quick-blocks");
  $("vc-print").addEventListener("click", () => printQuick(), { signal });
  // Ctrl+P やブラウザのメニューから刷るときも、早見表の面なら紙を組む
  window.addEventListener("beforeprint", () => {
    if (current(root) === "quick" && !document.body.classList.contains("vc-printing")) {
      buildPrint();
    }
  }, { signal });
  window.addEventListener("afterprint", endPrint, { signal });
  onLeave(endPrint);

  async function loadQuick(message = "") {
    try {
      const body = await api.get("/api/vc/quick");
      showMaster(body.master);
      renderQuick(body.quick);
      renderGrid(body.grid, message);
    } catch (err) {
      toastError(err);
    }
  }

  function renderQuick(view) {
    quickView = view;
    $("vc-quick-title").textContent = view.title;
    $("vc-quick-rev").textContent = view.revision_label;
    $("vc-quick-note").textContent =
      `長さは VC品種 の VC厚 から式で出しています(整数は${view.rounding})。`
      + "VC厚は 設定・管理者 → マスタ管理 → VC計算マスタ → VC品種 で直せます。";
    if (!view.blocks.length) {
      const p = document.createElement("p");
      p.className = "lead";
      p.textContent = "早見表の枠がありません。マスタ管理 → VC計算マスタ → 早見表ブロック で足してください。";
      blocksEl.replaceChildren(p);
      return;
    }
    // 列の数はすべての枠でそろえる(紙の早見表と同じく、列の位置が枠をまたいで揃う)
    blocksEl.replaceChildren(...view.blocks.map((b) => block(b, view.columns)));
  }

  function block(b, columns) {
    const box = document.createElement("article");
    box.className = "vc-qblock";
    box.dataset.tone = b.tone;

    const name = document.createElement("div");
    name.className = "vc-qblock__name";
    const vendor = document.createElement("span");
    vendor.textContent = b.vendor;
    const title = document.createElement("b");
    breakable(title, b.name);
    const source = document.createElement("small");
    source.className = "vc-qblock__src";
    // 固定値は「なぜそうなのか」も言う(知らない間に固定値になっていた、を残さない)
    source.textContent = b.source === "式" ? `式・VC厚 ${b.vcatu ?? "?"}` : "固定値(打った長さ)";
    if (b.source !== "式") {
      source.title = "VC品種と結びついていない枠です。マスに入れた長さをそのまま出します。"
        + "VC長さ計算 → 設定 → 選んだ枠を直す で「式にする」に切り替えられます";
    }
    name.append(vendor, title, source);
    if (b.problem) {
      const warn = document.createElement("small");
      warn.className = "vc-qblock__warn";
      warn.textContent = b.problem;
      name.append(warn);
    }

    const wrap = document.createElement("div");
    wrap.className = "vc-qblock__table";
    const table = document.createElement("table");
    table.className = "vc-qtable";
    const caption = document.createElement("caption");
    caption.textContent = `${b.vendor} ${b.name} の肉厚(mm)と長さ(m)`;
    caption.hidden = true;

    const head = document.createElement("tr");
    const corner = document.createElement("th");
    corner.className = "corner";
    corner.scope = "col";
    corner.textContent = "肉厚 mm";
    head.append(corner);
    for (let i = 0; i < columns; i += 1) {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = b.headers[i] || "";
      head.append(th);
    }
    const thead = document.createElement("thead");
    thead.append(head);

    const tbody = document.createElement("tbody");
    for (const row of b.rows) {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.scope = "row";
      th.textContent = row.label;
      tr.append(th);
      for (let i = 0; i < columns; i += 1) {
        const td = document.createElement("td");
        td.textContent = row.cells[i] || "";
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(caption, thead, tbody);
    wrap.append(table);
    box.append(name, wrap);
    return box;
  }

  // ---- 印刷用の紙(A4 横)を組む ----------------------------------------
  //   中身は紙の端から 12mm 内側だけ(`.vc-qpage`)。枠を1つずつ入れてみて、
  //   紙の内側からはみ出したら次の紙へ送る。枠の途中では切らない。
  //   **紙は body の直下に置く** ── 印刷のあいだ、画面のほかの部分(レール・帯)
  //   を隠すため(`body.vc-printing`)。刷り終えたら消す。
  function printQuick() {
    if (!quickView) return;
    buildPrint();
    window.print();
  }

  function buildPrint() {
    endPrint();
    const out = document.createElement("div");
    out.id = "vc-qprint";
    out.setAttribute("aria-hidden", "true");
    out.classList.add("is-measuring");            // 画面の外で組んで、高さを測る
    document.body.append(out);
    const blocks = [...blocksEl.querySelectorAll(".vc-qblock")];
    const pages = [];
    const newPage = () => {
      const page = document.createElement("section");
      page.className = "vc-qpage";
      if (!pages.length) {
        const head = document.querySelector(".vc-quick__head").cloneNode(true);
        for (const n of head.querySelectorAll("[id]")) n.removeAttribute("id");
        page.append(head);
      }
      const body = document.createElement("div");
      body.className = "vc-qpage__blocks";
      const foot = document.createElement("footer");
      foot.className = "vc-qpage__foot";
      const unit = document.createElement("span");
      unit.textContent = "肉厚 mm / 長さ m";
      const no = document.createElement("span");
      no.className = "vc-qpage__no";
      foot.append(unit, no);
      page.append(body, foot);
      out.append(page);
      pages.push(page);
      return body;
    };
    const fits = (body) => body.scrollHeight <= body.clientHeight + 1;
    let body = newPage();
    for (const b of blocks) {
      const clone = b.cloneNode(true);
      body.append(clone);
      if (!fits(body) && body.children.length > 1) {
        clone.remove();
        body = newPage();
        body.append(clone);                        // 1枚に入らない大きな枠は、そのまま
      }
    }
    pages.forEach((p, i) => {
      p.querySelector(".vc-qpage__no").textContent =
        pages.length > 1 ? `${i + 1} / ${pages.length}` : "";
    });
    out.classList.remove("is-measuring");
    document.body.classList.add("vc-printing");
  }

  function endPrint() {
    document.body.classList.remove("vc-printing");
    document.getElementById("vc-qprint")?.remove();
  }

  // ---- 設定: マスタの置き場所 ----------------------------------------------
  //   **どちらの名前を読んでいるか**(VC計算マスタ.sqlite3 → vc_master.sqlite3)
  //   を出す。決めるのはサーバ(`presenters/vc.place_view`)で、ここは描くだけ
  async function loadSettings(message = "") {
    try {
      const body = await api.get("/api/vc/settings");
      showMaster(body.master);
      renderPlace(body.place);
      renderGrid(body.grid, message);
    } catch (err) {
      toastError(err);
    }
  }

  function renderPlace(place) {
    if (!place) return;
    const status = $("vc-place-status");
    status.className = `msg msg--${place.level}`;
    status.textContent = place.status;
    $("vc-place-used").textContent = place.used;
    const rows = place.names.map((n) => {
      const tr = document.createElement("tr");
      const rank = document.createElement("td");
      rank.className = "num";
      rank.innerHTML = "<b></b>";
      rank.firstChild.textContent = n.rank;
      const name = document.createElement("td");
      name.innerHTML = "<code></code>";
      name.firstChild.textContent = n.name;
      const now = document.createElement("td");
      const badge = document.createElement("span");
      if (n.used) { badge.className = "badge badge--done"; badge.textContent = "読んでいます"; }
      else if (n.exists) { badge.className = "badge badge--todo"; badge.textContent = "あるが読んでいません"; }
      else { badge.className = "lead"; badge.textContent = "ありません"; }
      now.append(badge);
      const note = document.createElement("td");
      note.className = "lead";
      note.textContent = n.note;
      tr.append(rank, name, now, note);
      return tr;
    });
    $("vc-place-names").replaceChildren(...rows);
  }

  function placeNote(text, kind = "info") {
    const box = $("vc-place-note");
    box.hidden = !text;
    box.textContent = text || "";
    box.className = `msg msg--${kind}`;
  }

  // 置き場所を**この面の上で**保存する。保存先は参照設定の「VC計算マスタの
  // 置き場所」と同じ設定。断られたら(403)その場に合言葉の欄を出す
  const placePw = $("vc-place-pw");
  const showPlacePw = (on) => {
    placePw.hidden = !on;
    $("vc-place-pw-label").hidden = !on;
    if (on) placePw.focus();
  };
  $("vc-place-save").addEventListener("click", async () => {
    const key = $("vc-place").dataset.key;
    const payload = { [key]: $("vc-place-dir").value.trim() };
    if (placePw.value) payload.password = placePw.value;
    try {
      await api.post("/api/settings/paths", payload);
      placePw.value = "";                        // 合言葉は画面に残さない
      showPlacePw(false);
      placeNote("");
      toast("VC計算マスタの置き場所を保存しました", "ok");
      await loadSettings();
      reloadState();                             // 品種の一覧も新しい置き場所から
    } catch (err) {
      if (err.status === 403) showPlacePw(true);
      placeNote(err.message, "error");
    }
  }, { signal });

  // 早見表の面から「設定」へ(ページを読み直さずに面だけ替える)
  for (const link of document.querySelectorAll("[data-vc-tab]")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      selectTab(root, link.dataset.vcTab);    // `select` は品種を選ぶほう
    }, { signal });
  }

  // ---- 早見表の品種(管理者) ---------------------------------------------
  //   枠の一覧・品種を足す・選んだ枠を直す(行/列を消す・式⇔固定値・マスを足す)
  //   ・VC品種を消す。**決めるのはサーバ**(`nippou/vc/grid.py`)。ここは描いて、
  //   押す前に「何が消えるか」をサーバの数で確かめるだけ
  let gridBlocks = [];
  let gridProducts = [];

  const NEW_PRODUCT = "__new__";

  function renderGrid(view, message = "") {
    if (!view) return;
    gridBlocks = view.blocks || [];
    gridProducts = view.products || [];
    const pick = $("vc-grid-block");
    const was = pick.value;
    pick.replaceChildren(...gridBlocks.map((b) =>
      new Option(b.product ? `${b.name}(式: ${b.product})` : `${b.name}(固定値)`, b.name)));
    if (gridBlocks.some((b) => b.name === was)) pick.value = was;
    renderBlockList();
    renderAddProducts();
    renderDeleteProducts();
    fillGridHints();
    // 鍵が開いていればパスワードの欄は出さない(同じ合言葉を2度聞かない)
    $("vc-grid-password").hidden = view.can_edit;
    $("vc-grid-password-label").hidden = view.can_edit;
    $("vc-grid-unlock").hidden = view.can_edit;
    $("vc-grid-relock").hidden = !view.can_edit;
    $("vc-grid-lock").textContent = view.can_edit
      ? "管理者の鍵が開いています(マスタ管理と同じ鍵。閉めるまで続けて直せます)"
      : "「鍵を開ける」と、閉めるまで続けて直せます(押すたびにパスワードを入れても可)";
    for (const id of ["vc-grid-make", "vc-edit-delete", "vc-edit-source-save"]) {
      $(id).disabled = !gridBlocks.length;
    }
    if (!gridBlocks.length && !gridProducts.length) {
      note("早見表の枠を読めません(マスタで動いているときだけ使えます)。", "warn");
    } else if (message) note(message, "ok");
  }

  /** いまの枠の一覧。行の「直す」で下の欄に選び、「消す」で枠をマスごと消す。 */
  function renderBlockList() {
    const rows = gridBlocks.map((b) => {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      const mark = document.createElement("span");
      mark.className = "vc-blocks__tone";
      mark.dataset.tone = b.tone;
      name.append(mark, document.createTextNode(b.name));
      const kind = document.createElement("td");
      kind.className = b.product ? "vc-blocks__kind" : "vc-blocks__kind vc-blocks__kind--fixed";
      kind.textContent = b.kind;
      const insides = document.createElement("td");
      insides.textContent = b.insides.join(", ") || "─";
      const thicknesses = document.createElement("td");
      thicknesses.textContent = b.thicknesses.length
        ? `${b.thicknesses[0]}〜${b.thicknesses[b.thicknesses.length - 1]}(${b.thicknesses.length})`
        : "─";
      const cells = document.createElement("td");
      cells.className = "num";
      cells.textContent = b.empty ? `${b.cells}(空 ${b.empty})` : String(b.cells);
      const act = document.createElement("td");
      act.className = "vc-blocks__act";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "btn";
      edit.textContent = "直す";
      edit.addEventListener("click", () => {
        $("vc-grid-block").value = b.name;
        fillGridHints();
        $("vc-grid-block").scrollIntoView({ block: "center" });
        $("vc-grid-block").focus();
      });
      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "btn vc-btn-danger";
      drop.textContent = "消す";
      drop.title = b.product
        ? `枠「${b.name}」をマスごと消す(VC品種「${b.product}」は残す)`
        : `枠「${b.name}」をマスごと消す`;
      drop.addEventListener("click", () => deleteBlock(b));
      act.append(edit, drop);
      tr.append(name, kind, insides, thicknesses, cells, act);
      return tr;
    });
    $("vc-blocks").replaceChildren(...rows);
  }

  // ---- 足す ----
  function renderAddProducts() {
    const pick = $("vc-add-product");
    const was = pick.value;
    const options = [new Option("＋ 新しい VC品種を足す", NEW_PRODUCT)];
    for (const p of gridProducts.filter((x) => x.active)) {
      const has = gridBlocks.some((b) => b.name === p.name);
      options.push(new Option(
        `${p.name}(VC厚 ${p.vcatu}mm${has ? "・早見表にあり" : ""})`, p.name));
    }
    pick.replaceChildren(...options);
    if ([...pick.options].some((o) => o.value === was)) pick.value = was;
    showNewProduct();
  }

  function showNewProduct() {
    const isNew = $("vc-add-product").value === NEW_PRODUCT;
    $("vc-add-new").hidden = !isNew;
    $("vc-add-new-label").hidden = !isNew;
  }

  $("vc-add-product").addEventListener("change", showNewProduct, { signal });
  $("vc-add-make").addEventListener("click", async () => {
    const picked = $("vc-add-product").value;
    const isNew = picked === NEW_PRODUCT;
    const length = root.querySelector('input[name="vc-add-length"]:checked')?.value || "formula";
    const body = await send("/api/vc/quick-block", {
      product: isNew ? "" : picked,
      new_product: isNew ? $("vc-add-name").value : "",
      new_vcatu: isNew ? $("vc-add-vcatu").value : "",
      new_vendor: isNew ? $("vc-add-vendor").value : "",
      length,
      insides: $("vc-add-insides").value,
      thicknesses: $("vc-add-thicknesses").value,
      block: $("vc-add-block").value,
      tone: $("vc-add-tone").value,
    });
    if (!body) return;
    for (const id of ["vc-add-name", "vc-add-vcatu", "vc-add-vendor", "vc-add-insides",
      "vc-add-thicknesses", "vc-add-block"]) $(id).value = "";
    // 足した枠を「選んだ枠を直す」に選んでおく(続けて行を足す・消す)
    const added = gridBlocks[gridBlocks.length - 1];
    if (added) { $("vc-grid-block").value = added.name; fillGridHints(); }
    if (isNew) reloadState();                   // 計算の品種一覧にも出る
  }, { signal });

  // ---- 選んだ枠を直す ----
  const pickedBlock = () => gridBlocks.find((x) => x.name === $("vc-grid-block").value);

  /** 選んだ枠の長さの出し方・いまのマス・足す欄の例を出す(打つ手間を減らす)。
   *  **空のまま押せば、いまの内径・肉厚でそろえる**(空のマスに長さを入れるとき)。 */
  function fillGridHints() {
    const b = pickedBlock();
    $("vc-grid-insides").placeholder = b && b.insides.length
      ? `いま: ${b.insides.join(", ")}(空ならこのまま)` : "例: 87, 95";
    $("vc-grid-thicknesses").placeholder = b && b.thicknesses.length
      ? `いま: ${b.thicknesses.join(", ")}(空ならこのまま)` : "例: 5, 8, 10, 12, 14";
    fillKind(b);
    fillChips(b);
    fillLength(b);
  }

  /** 長さの出し方(式 / 固定値)を**いつも言う**。切り替えの選択肢も出す。 */
  function fillKind(b) {
    const kind = $("vc-edit-kind");
    kind.textContent = b ? b.kind : "";
    kind.className = b && !b.product ? "vc-edit-kind vc-edit-kind--fixed" : "vc-edit-kind";
    const pick = $("vc-edit-source");
    const options = [];
    if (b) {
      for (const p of gridProducts.filter((x) => x.active && x.name !== b.product)) {
        options.push(new Option(`式にする: VC品種「${p.name}」の VC厚 ${p.vcatu}mm`, `product:${p.name}`));
      }
      if (b.product) options.unshift(new Option("固定値にする(いま出ている数のまま)", "fixed"));
    }
    pick.replaceChildren(...options);
    // 固定値の枠は、同じ名前の品種があればそれを先に
    const same = b && !b.product && gridProducts.find((p) => p.name === b.name);
    if (same) pick.value = `product:${same.name}`;
    $("vc-edit-source-save").disabled = !options.length;
  }

  /** いまの内径・肉厚。× で、その行(列)のマスを消す。 */
  function fillChips(b) {
    const chips = (values, counts, axis) => values.map((v) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "btn vc-chip";
      chip.textContent = `${v} ×`;
      chip.title = axis === "inside"
        ? `内径 ${v} の行(マス ${counts[v] || 0})を消す`
        : `肉厚 ${v} の列(マス ${counts[v] || 0})を消す`;
      chip.addEventListener("click", () => deleteCells(b, axis, v, counts[v] || 0));
      return chip;
    });
    $("vc-edit-insides").replaceChildren(
      ...(b ? chips(b.insides, b.inside_cells || {}, "inside") : []));
    $("vc-edit-thicknesses").replaceChildren(
      ...(b ? chips(b.thicknesses, b.thickness_cells || {}, "thickness") : []));
  }

  /**
   * 足すマスの長さ。**計算品種のある枠は式で出すので何も選ばせない。**
   * 計算品種の無い枠(固定値)は、VC厚 を1つ決めれば同じ式で出して入れる
   * ── 借りる VC品種 を選ぶか、VC厚 を直に入れる。同じ名前の VC品種 があれば
   * それを先に選んでおく。
   */
  function fillLength(b) {
    const noteEl = $("vc-grid-length-note");
    const fixed = $("vc-grid-length-fixed");
    if (!b) { noteEl.textContent = ""; fixed.hidden = true; return; }
    if (b.product) {
      noteEl.textContent = `計算品種「${b.product}」の VC厚 から式で出します(長さは入れません)。`;
      fixed.hidden = true;
      return;
    }
    noteEl.textContent = "固定値の枠です。VC厚 を決めると、早見表と同じ式で"
      + "長さを出して入れます(マスタの早見表値を1マスずつ打たなくてよい)。"
      + (b.empty ? ` いま長さが空のマス: ${b.empty}` : "");
    fixed.hidden = false;
    const pick = $("vc-grid-length");
    const was = pick.value;
    const options = gridProducts.filter((p) => p.active).map((p) =>
      new Option(`VC品種「${p.name}」の VC厚(${p.vcatu}mm)で計算`, `product:${p.name}`));
    options.push(new Option("VC厚を入れて計算", "value"));
    options.push(new Option("長さは入れない(マスタで1マスずつ)", "none"));
    pick.replaceChildren(...options);
    const same = gridProducts.find((p) => p.name === b.name);
    if ([...pick.options].some((o) => o.value === was)) pick.value = was;
    else pick.value = same ? `product:${same.name}` : "value";
    $("vc-grid-vcatu-wrap").hidden = pick.value !== "value";
  }

  $("vc-grid-block").addEventListener("change", fillGridHints, { signal });
  $("vc-grid-length").addEventListener("change", () => {
    $("vc-grid-vcatu-wrap").hidden = $("vc-grid-length").value !== "value";
  }, { signal });

  $("vc-grid-make").addEventListener("click", async () => {
    const b = pickedBlock();
    const choice = $("vc-grid-length").value || "none";
    const length = !b || b.product ? "none"
      : choice.startsWith("product:") ? "product" : choice;
    const body = await send("/api/vc/quick-grid", {
      block: $("vc-grid-block").value,
      // 空なら、いまの内径・肉厚のまま(空のマスに長さを入れるだけのとき)
      insides: $("vc-grid-insides").value.trim() || (b ? b.insides.join(",") : ""),
      thicknesses: $("vc-grid-thicknesses").value.trim()
        || (b ? b.thicknesses.join(",") : ""),
      length,
      product: choice.startsWith("product:") ? choice.slice("product:".length) : "",
      vcatu: $("vc-grid-vcatu").value,
      fill_empty: $("vc-grid-fill").checked,
      overwrite: $("vc-grid-overwrite").checked,
    });
    if (body) { $("vc-grid-insides").value = ""; $("vc-grid-thicknesses").value = ""; }
  }, { signal });

  $("vc-edit-source-save").addEventListener("click", async () => {
    const b = pickedBlock();
    const choice = $("vc-edit-source").value;
    if (!b || !choice) return;
    const product = choice.startsWith("product:") ? choice.slice("product:".length) : "";
    if (!product && !confirm(`「${b.name}」を固定値にします。\n`
      + "いま早見表に出ている数を、そのまま長さとして入れます。"
      + "これからは VC厚 を直しても変わりません。よろしいですか?")) return;
    await send("/api/vc/quick-block/source", { block: b.name, product });
  }, { signal });

  $("vc-edit-delete").addEventListener("click", () => {
    const b = pickedBlock();
    if (b) deleteBlock(b);
  }, { signal });

  async function deleteBlock(b) {
    const keep = b.product ? `\nVC品種「${b.product}」は残ります(計算の品種一覧に出ます)。` : "";
    if (!confirm(`早見表から「${b.name}」の枠とマス ${b.cells} を消します。${keep}\nよろしいですか?`)) return;
    await send("/api/vc/quick-block/delete", { block: b.name });
  }

  async function deleteCells(b, axis, value, count) {
    const what = axis === "inside" ? `内径 ${value} の行` : `肉厚 ${value} の列`;
    if (!confirm(`「${b.name}」の ${what}(マス ${count})を消します。よろしいですか?`)) return;
    await send("/api/vc/quick-cells/delete", {
      block: b.name,
      insides: axis === "inside" ? value : "",
      thicknesses: axis === "thickness" ? value : "",
    });
  }

  // ---- VC品種を消す ----
  function renderDeleteProducts() {
    const pick = $("vc-del-product");
    const was = pick.value;
    pick.replaceChildren(...gridProducts.map((p) =>
      new Option(p.active ? p.name : `${p.name}(隠している)`, p.name)));
    if (gridProducts.some((p) => p.name === was)) pick.value = was;
    $("vc-del-product-go").disabled = !gridProducts.length;
    fillDeleteNote();
  }

  /** 一緒に消えるもの(サーバの数)。押す前に見えているようにする。 */
  function withProduct(p) {
    const parts = p.blocks.map((name) => {
      const b = gridBlocks.find((x) => x.name === name);
      return `早見表の枠「${name}」(マス ${b ? b.cells : "?"})`;
    });
    if (p.choices) parts.push(`内径の選択肢 ${p.choices}`);
    return parts;
  }

  function fillDeleteNote() {
    const p = gridProducts.find((x) => x.name === $("vc-del-product").value);
    if (!p) { $("vc-del-product-note").textContent = ""; return; }
    const parts = withProduct(p);
    $("vc-del-product-note").textContent = parts.length
      ? `一緒に消えるもの: ${parts.join("・")}`
      : "一緒に消えるものはありません(この品種を使っている枠はありません)。";
  }

  $("vc-del-product").addEventListener("change", fillDeleteNote, { signal });
  $("vc-del-product-go").addEventListener("click", async () => {
    const p = gridProducts.find((x) => x.name === $("vc-del-product").value);
    if (!p) return;
    const parts = withProduct(p);
    const also = parts.length ? `\n一緒に消えるもの: ${parts.join("・")}` : "";
    if (!confirm(`VC品種「${p.name}」を消します(計算の品種一覧からも消えます)。${also}\nよろしいですか?`)) return;
    const body = await send("/api/vc/product/delete", { product: p.name });
    if (body) reloadState();
  }, { signal });

  // ---- 鍵(マスタ管理と同じ)。1度開ければ、足す・消すを続けて押せる ----
  async function setKey(enable) {
    note("");
    try {
      const body = await api.post("/api/master/unlock",
        enable ? { enable: true, password: $("vc-grid-password").value } : { enable: false });
      $("vc-grid-password").value = "";          // 合言葉は画面に残さない
      toast(body.message, "ok");
      await loadSettings();
    } catch (err) {
      note(err.message, "error");
      $("vc-grid-password").focus();
    }
  }

  $("vc-grid-unlock").addEventListener("click", () => setKey(true), { signal });
  $("vc-grid-relock").addEventListener("click", () => setKey(false), { signal });
  $("vc-grid-password").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); setKey(true); }
  }, { signal });

  // ---- 共通 ----
  /** 書く口へ送り、返ってきた早見表と枠の一覧を描き直す。断られたら理由を出す。 */
  async function send(url, payload) {
    note("");
    try {
      const body = await api.post(url, { ...payload, password: $("vc-grid-password").value });
      $("vc-grid-password").value = "";          // 合言葉は画面に残さない
      renderQuick(body.quick);
      renderGrid(body.grid, body.message);
      toast(body.message, "ok");
      return body;
    } catch (err) {
      if (err instanceof ApiError && err.body && err.body.quick) {
        renderQuick(err.body.quick);
        renderGrid(err.body.grid);
      }
      note(err.message, "error");
      if (err.status === 403) $("vc-grid-password").focus();
      return null;
    }
  }

  function note(text, kind = "info") {
    const box = $("vc-grid-note");
    box.hidden = !text;
    box.textContent = text || "";
    box.className = `msg msg--${kind}`;
    if (text && kind === "error") box.scrollIntoView({ block: "nearest" });
  }
}
