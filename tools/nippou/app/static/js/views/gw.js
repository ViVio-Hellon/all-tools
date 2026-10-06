/*
  views/gw.js — 梱包資材重量計算

  計算式はサーバ(`logic/gw_calculation.py`)が持つ。ここは値を集めて
  送り、返ってきた数値を画面に写すだけ。

  【「引く」ボタンを外した】
  以前は LotNo を打ってから「LotNoで引く」を押す作りでした。**打ってから
  押す、を覚えていないと進めない**のは仕組みの都合で、押す人の都合では
  ありません。VBA も `LOT*_Change` で自動的に引いていました。
  ここも、欄を離れた時点で引きます。

  【オーダーNoは打つものではない】
  ロット番号が決まれば受注番号は決まっています(SIKAHIKI)。打ち直させ
  ないので、オーダーNoは**選択肢**にします ── 引当が1件ならそれが入り、
  複数なら選ばせます(VBAのフォームにも「LotNo入力後にオーダーNoを選択
  してください」と刷ってある)。
*/
import { api, tokenUrl } from "../api.js";
import { toast, toastError } from "../toast.js";

/* ---------------------------------------------------------------- */
/* 小さな道具                                                        */
/* ---------------------------------------------------------------- */
const byId = (id) => document.getElementById(id);
const val = (id) => byId(id)?.value ?? "";
const on = (id) => byId(id)?.checked ?? false;

function note(text, kind = "info") {
  const box = byId("search-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

function state(id, text) {
  const el = byId(id);
  if (el) el.textContent = text || "";
}

function dims() {
  const out = {};
  for (const el of document.querySelectorAll("[data-dim]")) {
    out[el.dataset.dim] = el.value;
  }
  return out;
}

function fill(map) {
  for (const [name, value] of Object.entries(map)) {
    const el = document.querySelector(`[data-dim="${name}"]`);
    if (el && value !== null && value !== undefined) el.value = value;
  }
}

/* ---------------------------------------------------------------- */
/* ① 製品情報                                                        */
/* ---------------------------------------------------------------- */

/** 引いて入った値。**打つ欄と混ぜない**ので、別の場所に文字で出す。 */
function showFacts(lot, order) {
  const put = (name, value) => {
    const el = byId(`pf-${name}`);
    if (!el) return;
    const text = value === null || value === undefined || value === ""
      ? "—" : String(value);
    el.textContent = text;
    el.dataset.empty = text === "—" ? "1" : "";
  };
  put("material", lot?.material);
  put("temper", lot?.temper);
  put("usage_code", lot?.usage_code);
  put("course", lot?.course);
  put("pack_spec_no", order?.pack_spec_no);
  put("unit_weight_kg", order?.unit_weight_kg);
  // 得意先・納入先・送り先は受注側が正(ロット側にも同名の列がある)
  put("customer", order?.customer || lot?.customer);
  put("delivery", order?.delivery || lot?.delivery);
  put("sender", order?.sender || lot?.sender);
}

/**
 * オーダーNo の選択肢を作る。
 *
 * **1件なら選ぶものが無い。** 選べる見た目にしておくと「他にもある」と
 * 読めてしまうので、そのときは選べなくする。
 */
function fillOrders(allocations) {
  const select = byId("order-no");
  if (!select) return;
  select.replaceChildren();
  const many = allocations.length > 1;

  if (!allocations.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "引当がありません";
    select.appendChild(opt);
    select.disabled = true;
    state("order-count", "");
    return;
  }
  if (many) {
    const head = document.createElement("option");
    head.value = "";
    head.textContent = "選んでください";
    select.appendChild(head);
  }
  for (const a of allocations) {
    const opt = document.createElement("option");
    opt.value = a.order_no;
    // 受注番号だけでは選べない。引当番号と数量も並べる
    opt.textContent = many ? a.label : a.order_no;
    opt.dataset.hikiNo = a.hiki_no || "";
    select.appendChild(opt);
  }
  select.disabled = !many;
  if (!many) select.value = allocations[0].order_no;
  state("order-count", many ? `(${allocations.length}件)` : "");
}

/*
  最後に引いたロット番号。**同じ番号で2度引かない。**

  7桁そろった時点(`input`)と欄を離れた時点(`blur`)の両方で引くので、
  押さえないと同じ番号を2回読みに行きます。それだけなら無駄なだけですが、
  **2度目がオーダーNoを選んだあとに届くと、選んで入った受注の値
  (包装仕様・単重)を空に戻します** ── 引当が複数のときは受注が決まって
  いないので、2度目の応答には受注が入っていないためです。
*/
let lastLot = "";
// 引いたロットの中身。**受注だけ引き直したときにも要る** ── 受注側の
// 応答にはロットの値(材質・調質・用途コード・コース)が入っていないので、
// 持っていないと、オーダーNoを選び直すたびにロット側が空に戻る
let lastLotInfo = null;
let lastGCourse = null;   // Gコースのロットの知らせ(サーバ `g_course`)。下書きにも入れる

/**
 * Gコースのロット(v4.17.0)。寸法の上に一言、寸法の欄に縁。
 * **言うことはサーバが決める**(`logic/g_course`)。null なら消す。
 */
function paintGCourse(g) {
  lastGCourse = g || null;
  const box = byId("g-course-note");
  if (box) {
    box.hidden = !g;
    const text = byId("g-course-text");
    if (text) text.textContent = g ? g.message : "";
  }
  for (const name of ["thickness_mm", "width_mm", "length_mm"]) {
    document.querySelector(`[data-dim="${name}"]`)
      ?.classList.toggle("is-gsize", !!(g && g.used_box));
  }
}

/** LotNo を離れたときに引く。**7桁そろってから**(打つ途中で読みに行かない)。 */
async function lookupLot() {
  // **全角で打たれても引けるようにする。**
  //
  // IME が全角のままだった・貼り付けた元が全角だった、はどちらも現場で
  // 起きます。`Ｎ７１３１Ｔ０` は見た目も長さも7桁なので**入力は成立
  // します**が、そのまま送ると `LS4LOT` に当たらず、オーダーNoが
  // いつまでも入ってきません ── 打った人には理由が見えません。
  //
  // 日報入力のLOT欄は前から直していました(`logic/input_rules.normalize`)。
  // こちらだけ抜けていたので、同じ直し方を当てます(NFKC + 大文字)。
  const box = document.getElementById("lot-no");
  const lotNo = val("lot-no").trim().normalize("NFKC").toUpperCase();
  // 直したものを欄にも戻す。**送った値と見えている値を食い違わせない**
  if (box && box.value !== lotNo) box.value = lotNo;
  if (!lotNo) { lastLot = ""; return; }
  if (lotNo.length !== 7) {
    lastLot = "";
    state("search-state", "7桁で引きます");
    return;
  }
  if (lotNo === lastLot) return;      // もう引いてある
  lastLot = lotNo;
  state("search-state", "引いています…");
  try {
    const body = await api.post("/api/gw/lot", { lot_no: lotNo });
    state("search-state", "");
    if (!body.found) {
      lastLotInfo = null;
      paintGCourse(null);
      fillOrders([]);
      showFacts(null, null);
      // **前のロットの資材を残さない。** 残すと、別のロットの
      // ＶＣ・合紙が付いたまま計算されます
      clearAuto();
      note(body.message, "warn");
      return;
    }
    note(body.message || "", body.message ? "warn" : "info");
    fillOrders(body.allocations || []);
    // オーダーが決まらないうちは、いったん白紙に戻す。
    // 決まれば下の `applyOrder` が入れ直します
    if (!body.order) clearAuto();
    // 寸法は**サーバが決めたもの**(Gコースなら BOX最終実績 ── v4.17.0)
    fill({
      thickness_mm: body.size.thickness_mm,
      width_mm: body.size.width_mm,
      length_mm: body.size.length_mm,
    });
    paintGCourse(body.g_course);
    lastLotInfo = body.lot;
    showFacts(body.lot, body.order);
    if (body.order) applyOrder(body.order, body.auto);
    saveDraft();
    toast(`ロット ${lotNo} を引きました`, "ok");
  } catch (err) {
    state("search-state", "");
    lastLot = "";                     // 失敗したら、次はもう一度引かせる
    toastError(err);
  }
}

/** コンボで選び直したとき。**受注が変われば資材の選択も変わる。** */
async function lookupOrder() {
  const orderNo = val("order-no").trim();
  if (!orderNo) return;
  try {
    const body = await api.post("/api/gw/order", {
      order_no: orderNo,
      vertical_bands: document.querySelector('[data-dim="vertical_bands"]')?.value ?? "",
    });
    if (!body.found) { note(body.message, "warn"); return; }
    note("");
    showFacts(lastLotInfo, body.order);
    applyOrder(body.order, body.auto);
    saveDraft();
    toast(`オーダー ${orderNo} を反映しました`, "ok");
  } catch (err) { toastError(err); }
}

function applyOrder(order, auto) {
  const spec = byId("pack-spec");
  if (spec) spec.value = order.pack_spec_no || "";
  applyAuto(auto);
}

/*
  オーダーから決まった梱包仕様を画面に入れる (VBA `VC選択`/`合紙選択`/
  `バンド選択` がチェックを付け直していたのと同じ)。

  **決めたのはサーバ。** ここは受け取った選択を入れて、`decided` に
  挙がっている欄に印を付けるだけ ── どれが自動で入ったのかが見えないと、
  人が直したものまで上書きされたように見える。
*/
/**
 * オーダーが決まっていないときに、**前のロットの選択を消す。**
 *
 * 【「オーダーNoがない状態でも使い資材にチェックが入った」】
 * `applyAuto` は `auto` が無ければ**何もしません**でした。何もしない
 * ということは、**前に引いたロットのチェックがそのまま残る**という
 * ことです ── 次のロットを引いて外れた(全角で打った・引当が無い・
 * 引当が複数で選ぶ前)とき、画面には前のロットの資材が付いたままに
 * なります。オーダーNoは空なのに資材だけ付いている、の正体はこれです。
 *
 * 【消すのは**オーダーから来たものだけ**】
 * 「使う資材」の7つのうち、オーダーで決まるのは**合紙と縦バンドアングルの
 * 2つだけ**です(縦バンドアングルは縦バンドの本数で決まる)。残りの5つ(ダンプレート・外装紙・バンド・ポリシート・
 * ハードボード)はどの梱包でも使う標準の資材で、画面は最初から付いた
 * 状態で配られます ── ここで全部外すと、**5つぶんの重量が抜けた**
 * 計算になり、直したつもりが別の間違いになります。
 *
 * なので戻すのは「配られたときの姿」です。ＶＣは外れた状態、合紙は
 * 付いた状態(＝画面の既定)、縦バンドアングルは縦バンドの本数どおり、
 * それに**自動で入れた印も消す**
 * ── 印が残っていると、人が選んだものまで自動で決まったように見えます。
 */
function clearAuto() {
  for (const id of ["use-vc", "vc-a", "vc-b"]) {
    const el = byId(id);
    if (el) el.checked = false;
  }
  for (const id of ["vc-name-a", "vc-name-b"]) {
    const el = byId(id);
    if (el) el.value = "";
  }
  // オーダーで決まるものを既定へ戻す。合紙は付いた状態、
  // 縦バンドアングルは縦バンドの本数どおり
  const interleaf = document.querySelector('[data-flag="interleaf"]');
  if (interleaf) interleaf.checked = true;
  syncAngle();
  const spec = byId("pack-spec");
  if (spec) spec.value = "";
  markDecided([]);
}

function applyAuto(auto) {
  if (!auto) { clearAuto(); return; }
  const check = (id, value) => {
    const el = byId(id);
    if (el) el.checked = !!value;
  };
  check("use-vc", auto.use_vc);
  check("vc-a", auto.vc_side_a);
  check("vc-b", auto.vc_side_b);
  const nameA = byId("vc-name-a");
  const nameB = byId("vc-name-b");
  if (nameA) nameA.value = auto.vc_name_a || "";
  if (nameB) nameB.value = auto.vc_name_b || "";

  // 合紙は SIKAODR の「合紙」列から決まる
  const interleaf = document.querySelector('[data-flag="interleaf"]');
  if (interleaf) interleaf.checked = !!auto.interleaf;
  const angle = document.querySelector('[data-flag="angle"]');
  if (angle && auto.decided.includes("angle")) angle.checked = !!auto.angle;

  const band = byId("band-kind");
  if (band && auto.band_kind) band.value = auto.band_kind;
  if (auto.vertical_bands !== null && auto.vertical_bands !== undefined) {
    fill({ vertical_bands: auto.vertical_bands });
    syncAngle();
  }

  markDecided(auto.decided || []);
  if (auto.note) note(`自動で選びました(${auto.note})`, "info");
}

// 自動で決まった欄に印を付ける。VBA が赤色でしていたこと
const DECIDED_TARGETS = {
  vc: ["use-vc", "vc-a", "vc-b", "vc-name-a", "vc-name-b"],
  interleaf: ['[data-flag-for="interleaf"]'],
  angle: ['[data-flag-for="angle"]'],
  band_kind: ["band-kind"],
  vertical_bands: ['[data-dim="vertical_bands"]'],
};

function pick(sel) {
  return sel.startsWith("[") ? document.querySelector(sel) : byId(sel);
}

function markDecided(decided) {
  for (const selectors of Object.values(DECIDED_TARGETS)) {
    for (const sel of selectors) pick(sel)?.classList.remove("is-auto");
  }
  for (const name of decided) {
    for (const sel of DECIDED_TARGETS[name] || []) {
      pick(sel)?.classList.add("is-auto");
    }
  }
}

/* ---------------------------------------------------------------- */
/* ④ 結果                                                            */
/* ---------------------------------------------------------------- */
function showResult(body) {
  const put = (name, value) => {
    const cell = document.querySelector(
      `[data-result="${name}"] [data-result-value]`);
    if (!cell) return;
    const row = cell.closest("[data-result]");
    if (value === null || value === undefined) {
      cell.textContent = "—";
      if (row) row.dataset.empty = "1";
      return;
    }
    cell.textContent = Number(value).toFixed(2);
    if (row) delete row.dataset.empty;
  };

  if (!body) {
    for (const row of document.querySelectorAll("[data-result]")) {
      row.querySelector("[data-result-value]").textContent = "—";
      row.dataset.empty = "1";
    }
    state("stack-count", "");
    return;
  }
  for (const [name, value] of Object.entries(body.weights || {})) put(name, value);
  put("material_total", body.material_total);
  put("tare_weight", body.tare_weight);
  // Aインプット重量が空/0のときは GW を出さない(VBAは欄ごと隠していた)
  put("gross_weight", body.gross_weight);
  state("stack-count", `積み枚数 ${body.stack_count}`);
}

function flags() {
  const out = {};
  for (const el of document.querySelectorAll("[data-flag]")) {
    out[el.dataset.flag] = el.checked;
  }
  return out;
}

function payload() {
  return {
    ...dims(),
    flags: flags(),
    band_kind: val("band-kind"),
    stack_pattern: val("stack-pattern"),
    use_vc: on("use-vc"),
    vc_side_a: on("vc-a"),
    vc_side_b: on("vc-b"),
    vc_name_a: val("vc-name-a"),
    vc_name_b: val("vc-name-b"),
    input_weight_kg: val("input-weight"),
    use_combined_load: on("use-combined"),
    combined_load_kg: val("combined-load"),
    pack_spec_no: val("pack-spec"),
  };
}

/* ---------------------------------------------------------------- */
/* 梱包図 (VBA `図形展開`)                                            */
/*                                                                    */
/* **何を描くかはサーバ**(`logic/packing_figure.py`)が cm の箱で返す。 */
/* ここは箱を SVG の座標へ写すだけ ── グラフ(`chart.js`)と同じ分け方。*/
/*                                                                    */
/* 奥行きは45度に倒して半分に見せる(キャビネット図)。VBA が座標に      */
/* 掛けていた 0.354 と同じもので、サーバが `depth_ratio` で渡す。      */
/* ---------------------------------------------------------------- */
const SVG_NS = "http://www.w3.org/2000/svg";
const FIG = { width: 460, height: 340, pad: 28 };

function svgEl(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

/** 直方体1つ。手前の面・上の面・右の面の3枚で立体に見せる。 */
function boxFaces(box, ratio, at) {
  const d = box.depth * ratio;
  const [x, y] = at(box.x, box.y);
  const [x2, y2] = at(box.x + box.w, box.y + box.h);
  const [dx, dy] = [at(d, 0)[0] - at(0, 0)[0], at(0, d)[1] - at(0, 0)[1]];
  // 材質(バンドの PET / 帯鉄)は**サーバが言う**。色は CSS が持つ
  const tone = box.tone ? ` fig-box--${box.kind}-${box.tone}` : "";
  const g = svgEl("g", { class: `fig-box fig-box--${box.kind}${tone}` });
  // 奥から: 上の面 → 右の面 → 手前の面(手前が最後=いちばん上に出る)
  g.appendChild(svgEl("polygon", {
    class: "fig-face fig-face--top",
    points: `${x},${y} ${x2},${y} ${x2 + dx},${y - dy} ${x + dx},${y - dy}`,
  }));
  g.appendChild(svgEl("polygon", {
    class: "fig-face fig-face--side",
    points: `${x2},${y} ${x2 + dx},${y - dy} ${x2 + dx},${y2 - dy} ${x2},${y2}`,
  }));
  g.appendChild(svgEl("rect", {
    class: "fig-face fig-face--front",
    x: Math.min(x, x2), y: Math.min(y, y2),
    width: Math.abs(x2 - x), height: Math.abs(y2 - y),
  }));
  if (box.label) g.appendChild(svgEl("title")).textContent = box.label;
  return g;
}

/** 梱包図を描く。**箱の並び順がそのまま描く順**(奥から手前へ)。 */
function paintFigure(figure) {
  const card = byId("figure");
  const canvas = byId("figure-canvas");
  const problems = byId("figure-problems");
  const facts = byId("figure-facts");
  if (!card || !canvas) return;

  if (!figure) { card.hidden = true; return; }
  card.hidden = false;
  canvas.replaceChildren();
  facts.replaceChildren();

  // 描けない理由は**字で出す**。図の代わりに空欄を出しても伝わらない
  const reasons = figure.problems || [];
  problems.hidden = reasons.length === 0;
  problems.textContent = reasons.length
    ? `この入力では図を描けません ── ${reasons.map((p) => p.message).join(" ")}`
    : "";
  if (!figure.drawable) return;

  const boxes = figure.boxes || [];
  const ratio = figure.depth_ratio ?? 0.354;
  // 箱ぜんぶが入る範囲を採る(奥行きで上と右へはみ出すぶんも込み)
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const b of boxes) {
    const d = b.depth * ratio;
    minX = Math.min(minX, b.x);
    maxX = Math.max(maxX, b.x + b.w + d);
    minY = Math.min(minY, b.y - d);
    maxY = Math.max(maxY, b.y + b.h);
  }
  const spanX = Math.max(1e-6, maxX - minX);
  const spanY = Math.max(1e-6, maxY - minY);
  const scale = Math.min((FIG.width - FIG.pad * 2) / spanX,
                         (FIG.height - FIG.pad * 2) / spanY);
  // 中央に寄せる
  const offX = FIG.pad + (FIG.width - FIG.pad * 2 - spanX * scale) / 2;
  const offY = FIG.pad + (FIG.height - FIG.pad * 2 - spanY * scale) / 2;
  const at = (cx, cy) => [offX + (cx - minX) * scale, offY + (cy - minY) * scale];

  const svg = svgEl("svg", {
    class: "fig", viewBox: `0 0 ${FIG.width} ${FIG.height}`,
    role: "img",
    "aria-label": `梱包の外形 幅${figure.width_cm}cm 高さ${figure.height_cm}cm`
                  + ` 奥行${figure.depth_cm}cm`,
  });
  for (const box of boxes) svg.appendChild(boxFaces(box, ratio, at));
  canvas.appendChild(svg);

  // **形だけに頼らない。** 図から読み取るものを字でも並べる
  for (const fact of figure.facts || []) {
    const wrap = document.createElement("div");
    wrap.className = "fact";
    const dt = document.createElement("dt");
    dt.textContent = fact.label;
    const dd = document.createElement("dd");
    dd.textContent = fact.value;
    wrap.append(dt, dd);
    facts.appendChild(wrap);
  }
}

/* ---------------------------------------------------------------- */
/* 印刷 (VBA `UFGW` の印刷ボタン → `印刷()` / `印刷2()`)               */
/*                                                                    */
/* **紙を組み立てるのはサーバ**(`reporting/gw_print.py`)。ここは、     */
/* いま画面に入っているものを鍵にして窓を開けるだけ ── 計算結果は      */
/* どこにも保存していないので、入力そのものが鍵になる。                */
/*                                                                    */
/* 梱包数だけは紙のための値で、計算には効かない(1梱包ぶんに掛ける     */
/* だけ)。だから計算し直さずに、窓を開けるときだけ足す。              */
/* ---------------------------------------------------------------- */

/** 紙の鍵。`/api/gw/calculate` に送るのと**同じ名前**でクエリにする。 */
function printParams() {
  const params = new URLSearchParams();
  const body = payload();
  for (const [key, value] of Object.entries(body)) {
    if (key === "flags") continue;
    if (value === null || value === undefined || value === "") continue;
    params.append(key, typeof value === "boolean" ? (value ? "1" : "0") : value);
  }
  // 資材のチェックは ON のものだけを並べる。7つ別々の欄にすると
  // URL が読めなくなるので(サーバ側も同じ形で読む)
  params.append("flags", Object.entries(body.flags || {})
    .filter(([, checked]) => checked).map(([key]) => key).join(","));

  // 引いて入った値(製品情報)。**画面に出ているものだけ**送る ──
  // 「—」は「まだ引いていない」の印なので、紙に写さない
  params.append("lot_no", val("lot-no"));
  params.append("order_no", val("order-no"));
  for (const dd of document.querySelectorAll('#product-facts dd[id^="pf-"]')) {
    const text = (dd.textContent || "").trim();
    if (text && text !== "—") params.append(dd.id.slice(3), text);
  }
  // Gコースのロットなら、紙にもそう書く(v4.17.0。値はサーバが返した控え)
  if (lastGCourse) params.append("g_course", lastGCourse.stored);
  return params;
}

/**
 * 印刷を押せるかどうか。**計算してからでないと押させない。**
 *
 * 押せない理由は横に字で出す(`#print-why`)。吹き出し(`title`)だけだと、
 * マウスを載せない人には「ボタンになっていない」としか見えなかった。
 */
function refreshPrint(ready) {
  const btn = byId("print-gw");
  if (!btn) return;
  if (ready !== undefined) btn.dataset.ready = ready ? "1" : "";
  const calculated = btn.dataset.ready === "1";
  btn.disabled = !calculated;
  btn.title = calculated ? "" : "先に「計算」を押してください";
  const why = byId("print-why");
  if (why) why.hidden = calculated;
}

/** 刷れる梱包数の上限。**画面の表と同じ範囲**(`gw_print.MAX_PACKS`)。 */
const PACK_MAX = 100;

function openPrint() {
  const params = printParams();
  // 梱包数は**紙だけの値**。計算そのものには効かない(掛け算するだけ)
  params.append("packs", val("print-packs") || "1");
  // 開いたら**そのまま印刷の画面を出す**(紙の窓が自分で呼ぶ)。
  // Ctrl+P を知らない人にも刷れるように
  params.append("print", "1");
  // ヘッダを付けられない開き方なので、トークンはクエリに(紙の窓と同じ)
  window.open(tokenUrl(`/report/gw?${params.toString()}`), "_blank", "noopener");
}

/*
  断られた理由を全部出し、間違っている欄に印を付ける。

  VBA は1つ見つけるたびに MsgBox して止めていたので、3か所間違って
  いると3回押し直すことになった。**サーバは全部返す**ので、ここも全部出す。
*/
function showProblems(problems) {
  for (const el of document.querySelectorAll(".is-bad")) {
    el.classList.remove("is-bad");
  }
  const box = byId("calc-problems");
  if (!box) return;
  if (!problems || !problems.length) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  const list = document.createElement("ul");
  for (const p of problems) {
    const li = document.createElement("li");
    li.textContent = p.message;
    list.appendChild(li);
    const el = document.querySelector(`[data-dim="${p.field}"]`)
      || byId(p.field.replace(/_/g, "-"));
    if (el) el.classList.add("is-bad");
  }
  box.replaceChildren(list);
  box.hidden = false;
}

let perPack = null;

function renderPerPack() {
  const card = byId("per-pack");
  const table = byId("per-pack-table");
  if (!card || !table || !perPack) return;
  const limit = Number(val("per-pack-rows") || 20);

  const head = byId("per-pack-head");
  head.replaceChildren();
  const first = document.createElement("th");
  first.textContent = "梱包数";
  head.appendChild(first);
  for (const name of perPack.columns) {
    const th = document.createElement("th");
    th.className = "num";
    th.textContent = name;
    head.appendChild(th);
  }

  const body = table.tBodies[0];
  body.replaceChildren();
  perPack.rows.slice(0, limit).forEach((row, i) => {
    const tr = body.insertRow();
    const label = tr.insertCell();
    label.textContent = `${i + 1}`;
    label.className = "num";
    for (const value of row) {
      const td = tr.insertCell();
      td.className = "num";
      td.textContent = value.toFixed(2);
    }
  });
  card.hidden = false;
}

/* ================================================================
   計算の内訳

   **組み立てるのはサーバ**(`presenters/gw_breakdown.py`)。式も、数字を
   入れた式も、出どころも文字列で降りてくるので、ここは並べるだけです ──
   JS が式を組み立てると、実際の計算(Python)と2か所に分かれます。
   ================================================================ */
let breakdownText = "";

function pairRows(tbody, rows) {
  tbody.replaceChildren();
  for (const row of rows) {
    const tr = tbody.insertRow();
    tr.insertCell().textContent = row.label;
    const value = tr.insertCell();
    value.innerHTML = "";
    const b = document.createElement("b");
    b.textContent = row.value;
    value.appendChild(b);
    if (row.note) {
      const note = document.createElement("span");
      note.className = "lead";
      note.textContent = ` ${row.note}`;
      value.appendChild(note);
    }
  }
}

/** 資材1つぶん。**4段を上から順に。** */
function lineCard(line) {
  const box = document.createElement("div");
  box.className = "bd-line";
  if (!line.used) box.dataset.skipped = "1";

  const head = document.createElement("div");
  head.className = "bd-line__head";
  const name = document.createElement("b");
  name.textContent = line.label;
  const kg = document.createElement("span");
  kg.className = "bd-line__kg";
  kg.textContent = line.used ? `${line.weight.toFixed(2)} kg` : "0 kg";
  head.append(name, kg);
  box.appendChild(head);

  if (!line.used) {
    const why = document.createElement("p");
    why.className = "lead";
    why.textContent = line.skipped;
    box.appendChild(why);
    return box;
  }

  for (const [label, text, mono] of [
    ["式", line.formula, false],
    ["数字", line.substituted, true],
    ["", line.last_step, true],
    ["出どころ", line.source, false],
  ]) {
    if (!text) continue;
    const row = document.createElement("div");
    row.className = "bd-step";
    const tag = document.createElement("span");
    tag.className = "bd-step__tag";
    tag.textContent = label;
    const body = document.createElement("span");
    if (mono) body.className = "bd-step__mono";
    body.textContent = text;
    row.append(tag, body);
    box.appendChild(row);
  }
  if (line.note) {
    const note = document.createElement("p");
    note.className = "lead";
    note.textContent = line.note;
    box.appendChild(note);
  }
  return box;
}

function renderBreakdown(data, text) {
  const card = byId("breakdown");
  if (!card) return;
  if (!data) { card.hidden = true; breakdownText = ""; return; }
  breakdownText = text || "";

  pairRows(byId("bd-inputs"), data.inputs || []);
  pairRows(byId("bd-common"), data.common || []);

  const lines = byId("bd-lines");
  lines.replaceChildren();
  for (const line of data.lines || []) lines.appendChild(lineCard(line));

  const totals = byId("bd-totals");
  totals.replaceChildren();
  for (const total of data.totals || []) {
    const tr = totals.insertRow();
    tr.insertCell().textContent = total.label;
    tr.insertCell().textContent = total.formula;
    const sub = tr.insertCell();
    sub.className = "bd-step__mono";
    sub.textContent = total.substituted;
    const value = tr.insertCell();
    value.className = "num";
    value.textContent = total.value === null
      ? (total.note || "―") : total.value.toFixed(2);
  }

  const notes = byId("bd-notes");
  notes.replaceChildren();
  for (const note of data.notes || []) {
    const li = document.createElement("li");
    li.textContent = note;
    notes.appendChild(li);
  }
  card.hidden = false;
}

/**
 * 梱包図を出す (VBA `図形展開` のボタン)。
 *
 * 図そのものは計算すると下に出ますが、**画面の下のほうにあるので
 * 気づかれませんでした**(「図形描写出すボタンがない」)。VBA には押す
 * ボタンがあったので、同じものを計算の隣に置きます。
 *
 * まだ計算していなければ**先に計算します** ── 押したのに何も起きない、
 * を作らないため。描けない入力のときは、その理由が図の枠に出ます
 * (`paintFigure`)。
 *
 * 計算そのものが通らなかったとき(寸法が空、など)は図の枠すら出ません。
 * **そこでも黙って終わらない** ── 断りの中身は「直すところ」
 * (`#calc-problems`)に出ているので、そこへ送って、押した結果が
 * どこにあるのかを言います。
 */
async function showFigure() {
  const card = byId("figure");
  if (!card) return;
  if (card.hidden) await calculate();
  if (card.hidden) {
    state("calc-state", "まだ図を描けません。直すところを見てください");
    byId("calc-problems")?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function calculate() {
  state("calc-state", "計算しています…");
  try {
    const body = await api.post("/api/gw/calculate", payload());
    showProblems([]);
    showResult(body);
    perPack = body.per_pack;
    renderPerPack();
    renderBreakdown(body.breakdown, body.breakdown_text);
    paintFigure(body.figure);
    state("calc-state", "");
    // 計算が通ったので、この数字の紙を刷れる
    refreshPrint(true);
    saveDraft();
    toast(body.message || "計算しました", "ok");
  } catch (err) {
    state("calc-state", "");
    showResult(null);
    byId("per-pack").hidden = true;
    renderBreakdown(null);
    paintFigure(null);
    // **断られた入力の紙を刷らせない。** 空欄だらけの紙を出してから
    // 気づくことになる
    refreshPrint(false);
    // 断りの中身(`problems`)はサーバが付けてくる。無ければトーストだけ
    showProblems(err.body?.problems);
    if (!err.body?.problems?.length) toastError(err);
  }
}

/* ---------------------------------------------------------------- */
/* 入力値を覚えておく / まとめて消す                                  */
/*                                                                    */
/*     マスタを見て戻ったりすると入力値が消えている                   */
/*     入力値クリアボタンを作ってください                             */
/*                                                                    */
/* 画面を移ると中身が作り直されるので、打った値は消えていました。     */
/* **このタブの中だけ**(`sessionStorage`)に下書きとして覚え、戻って   */
/* きたら入れ直して、計算してあったなら計算し直します。共有DBには書き */
/* ません ── 業務の記録ではなく、打ちかけの控えなので。               */
/* ---------------------------------------------------------------- */
const DRAFT_KEY = "gw:draft";
// 覚える欄(寸法・資材のチェックは data 属性で拾う)
const DRAFT_IDS = ["lot-no", "band-kind", "stack-pattern", "pack-spec", "use-vc",
                   "vc-a", "vc-b", "vc-name-a", "vc-name-b", "input-weight",
                   "use-combined", "combined-load"];
// 画面が配られたときの姿(クリアで戻す先)
let pristine = null;
let restoring = false;

function draftFields() {
  const out = [];
  for (const id of DRAFT_IDS) {
    const el = byId(id);
    if (el) out.push([`#${id}`, el]);
  }
  for (const el of document.querySelectorAll("[data-dim]")) out.push([`dim:${el.dataset.dim}`, el]);
  for (const el of document.querySelectorAll("[data-flag]")) out.push([`flag:${el.dataset.flag}`, el]);
  return out;
}

function snapshot() {
  const values = {};
  for (const [key, el] of draftFields()) {
    values[key] = el.type === "checkbox" ? el.checked : el.value;
  }
  const order = byId("order-no");
  const facts = {};
  for (const dd of document.querySelectorAll('[id^="pf-"]')) facts[dd.id] = dd.textContent;
  return {
    values,
    orders: order ? [...order.options].map((o) => [o.value, o.textContent, o.dataset.hikiNo || ""]) : [],
    order: order?.value || "",
    orderDisabled: order ? order.disabled : true,
    facts,
    lotInfo: lastLotInfo,
    gCourse: lastGCourse,
    calculated: byId("print-gw")?.dataset.ready === "1",
  };
}

function putSnapshot(snap) {
  for (const [key, el] of draftFields()) {
    if (!(key in snap.values)) continue;
    if (el.type === "checkbox") el.checked = !!snap.values[key];
    else el.value = snap.values[key] ?? "";
  }
  const order = byId("order-no");
  if (order) {
    order.replaceChildren();
    for (const [value, text, hiki] of snap.orders || []) {
      const opt = document.createElement("option");
      opt.value = value; opt.textContent = text;
      if (hiki) opt.dataset.hikiNo = hiki;
      order.appendChild(opt);
    }
    order.value = snap.order || "";
    order.disabled = !!snap.orderDisabled;
  }
  for (const [id, text] of Object.entries(snap.facts || {})) {
    const dd = byId(id);
    if (dd) { dd.textContent = text; dd.dataset.empty = text === "—" ? "1" : ""; }
  }
  lastLotInfo = snap.lotInfo || null;
  paintGCourse(snap.gCourse || null);
  // 入れ直したロットで**引き直さない**(引き直すと、直した資材が戻される)
  lastLot = (byId("lot-no")?.value || "").trim();
  const combined = byId("combined-load");
  if (combined) combined.disabled = !byId("use-combined")?.checked;
}

function saveDraft() {
  if (restoring) return;
  try { sessionStorage.setItem(DRAFT_KEY, JSON.stringify(snapshot())); } catch { /* 覚えられないだけ */ }
}

function loadDraft() {
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function dropDraft() {
  try { sessionStorage.removeItem(DRAFT_KEY); } catch { /* 無ければそれでよい */ }
}

/** 入力値をまとめて消して、**配られたときの姿**へ戻す。 */
function clearInputs() {
  if (!pristine) return;
  if (!confirm("入力した値をすべて消して、最初の状態に戻します。よろしいですか?")) return;
  restoring = true;
  putSnapshot(pristine);
  restoring = false;
  lastLot = "";
  lastLotInfo = null;
  markDecided([]);
  showProblems([]);
  showResult(null);
  perPack = null;
  const perPackBox = byId("per-pack");
  if (perPackBox) perPackBox.hidden = true;
  renderBreakdown(null);
  paintFigure(null);
  refreshPrint(false);
  note("");
  state("search-state", "");
  state("order-count", "");
  dropDraft();
  byId("lot-no")?.focus();
  toast("入力値を消しました", "ok");
}

/** 縦バンドアングルは**縦バンドを掛けるときだけ**(サーバの決まりと同じ)。 */
function syncAngle() {
  const bands = Number(document.querySelector('[data-dim="vertical_bands"]')?.value || 0);
  const angle = document.querySelector('[data-flag="angle"]');
  if (angle) angle.checked = Number.isFinite(bands) && bands >= 1;
}

/* ---------------------------------------------------------------- */
/* 配線                                                               */
/*                                                                    */
/* **画面へ来るたびに繋ぎ直す。** ES モジュールは一度しか読まれないので、 */
/* 差し替えで作り直された要素には、前回付けた listener が残っていない。  */
/* ---------------------------------------------------------------- */
/* ---------------------------------------------------------------- */
/* スピンボタン(v4.16.0)── 梱包締枚数・縦バンド本数・横バンド本数    */
/* ---------------------------------------------------------------- */
/** 1つ増やす / 減らす。**打った値から**数え、下限・上限で止める。
 *  打ったときと同じ通り道(input / change)を流すので、下書き・角当ての同期もそのまま効く */
function stepSpin(input, step) {
  if (!input || input.disabled) return;
  const low = input.dataset.spinMin === undefined ? null : Number(input.dataset.spinMin);
  const high = input.dataset.spinMax === undefined ? null : Number(input.dataset.spinMax);
  // 空の欄は 0 から数える(バンドは ▲ 1回で 1 本。締枚数は下限の 1 に止まる)
  const now = Number.parseInt(String(input.value || "").normalize("NFKC"), 10);
  let next = (Number.isFinite(now) ? now : 0) + step;
  if (low !== null) next = Math.max(low, next);
  if (high !== null) next = Math.min(high, next);
  input.value = String(next);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function wireSpins() {
  for (const btn of document.querySelectorAll("[data-spin]")) {
    btn.addEventListener("click", (event) => {
      event.preventDefault();          // 欄の label へ押したことを流さない
      stepSpin(btn.closest(".spin")?.querySelector("input"), Number(btn.dataset.spin));
    });
  }
  // 上下の矢印キーでも(片手で数を合わせる)
  for (const input of document.querySelectorAll(".spin input")) {
    input.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      stepSpin(input, event.key === "ArrowUp" ? 1 : -1);
    });
  }
}

export function start() {
  // 前の画面のぶんは持ち越さない
  perPack = null;
  breakdownText = "";
  lastLot = "";
  lastLotInfo = null;
  lastGCourse = null;
  showResult(null);
  renderBreakdown(null);
  paintFigure(null);
  // 来たばかりの画面には計算結果が無い。**押せない状態から始める**
  refreshPrint(false);

  // 配られた姿を控えてから、**このタブで打ちかけていた値を入れ直す**
  pristine = snapshot();
  const draft = loadDraft();
  if (draft) {
    restoring = true;
    putSnapshot(draft);
    restoring = false;
    if (draft.calculated) calculate();
    note("前に打っていた値を入れ直しました(「入力値クリア」で消せます)", "info");
  }
  // 打つたび・選ぶたびに控える。**欄そのものに付ける** ── 画面の外枠に
  // 付けると、ほかの画面で打ったものまで拾って下書きを上書きする
  for (const [, el] of draftFields()) {
    el.addEventListener("input", saveDraft);
    el.addEventListener("change", saveDraft);
  }
  byId("order-no")?.addEventListener("change", saveDraft);
  byId("clear-inputs")?.addEventListener("click", clearInputs);
  wireSpins();
  document.querySelector('[data-dim="vertical_bands"]')?.addEventListener("change", () => {
    syncAngle();
    saveDraft();
  });

  byId("print-gw")?.addEventListener("click", openPrint);
  // 梱包数は数字だけ。**打ちながら弾く**(打ってから断られるより早い)
  byId("print-packs")?.addEventListener("input", (e) => {
    e.target.value = e.target.value.replace(/[^0-9]/g, "");
  });
  /*
    欄を離れたら、刷れる範囲(1〜100梱包)へそろえる。

    **黙って丸めない。** サーバも同じ範囲へ丸めますが、画面の欄に150が
    残ったまま100の紙が出ると、押した人には理由が分かりません。ここで
    欄の数字ごと直せば、見えているものと出るものが一致します。
  */
  byId("print-packs")?.addEventListener("blur", (e) => {
    const packs = Number(e.target.value);
    if (!Number.isFinite(packs) || packs < 1) { e.target.value = "1"; return; }
    if (packs > PACK_MAX) {
      e.target.value = String(PACK_MAX);
      toast(`梱包数は${PACK_MAX}梱包までです`, "warn");
    }
  });

  /*
    内訳を文字にしてコピーする。**数字を疑われたときに、そのまま送れるように。**
    文面はサーバが組み立てたもの(`Breakdown.as_text`)をそのまま使う ──
    画面で作り直すと、写したものと画面が食い違う。
  */
  byId("breakdown-copy")?.addEventListener("click", async () => {
    if (!breakdownText) { toast("先に計算してください", "warn"); return; }
    try {
      await navigator.clipboard.writeText(breakdownText);
      toast("内訳をコピーしました。メールやチャットに貼り付けられます", "ok");
    } catch (err) {
      // コピーを許さない設定のブラウザもある。**黙って終わらせない**
      toast("コピーできませんでした。画面の内訳をそのままお読みください", "warn");
    }
  });

  // ---- ① 引くのは自動。ボタンは無い ----
  const lot = byId("lot-no");
  lot?.addEventListener("blur", lookupLot);
  // 7桁そろった時点でも引く(VBA `LOT*_Change` と同じ)。欄を離れずに
  // 次を見たい人が、いちいち Tab を押さずに済む
  lot?.addEventListener("input", () => {
    if (lot.value.trim().length === 7) lookupLot();
    else state("search-state", "");
  });
  byId("order-no")?.addEventListener("change", lookupOrder);

  // ---- ② 積合せは「有り」のときだけ打てる ----
  const combined = byId("use-combined");
  const combinedValue = byId("combined-load");
  const syncCombined = () => {
    if (combinedValue) combinedValue.disabled = !combined?.checked;
  };
  combined?.addEventListener("change", syncCombined);
  syncCombined();

  byId("per-pack-rows")?.addEventListener("change", renderPerPack);
  byId("calc")?.addEventListener("click", calculate);
  byId("show-figure")?.addEventListener("click", showFigure);
}
