/*
  views/entry.js — 日報入力画面

  **業務の判断はサーバが持つ。** ここがやるのは3つだけ:
    1. 入力欄から値を集める
    2. サーバへ送る
    3. 返ってきたビューモデルを画面へ写す

  「CON が 0 なら単重は空」のような規則をここへ書かない ── 書いた瞬間、
  テストのある `logic/calculations.py` と分かれる。
*/

import { api, tokenUrl } from "../api.js";
import { openPage } from "../desktop.js";
import { lineLabel } from "../line_label.js";
import { toast, toastError } from "../toast.js";
import { beforeLeave, pageSignal, refresh } from "../nav.js";
import { paintRibbon, showShiftMoved } from "../ribbon.js";
import { playKey } from "../sound.js";
// 開閉と焦点の戻しは1か所に置く(作業者選択・全停入力と同じ作り)
import { open as openModal, close as closeModal } from "./modals.js";
// **打てるタブは1枚だけ。** 2枚に打つと、後から保存したほうで上書きされ、
// 先に打ったぶんが黙って消える(`tab_lock.js` に理由を書いてあります)
import * as tabLock from "../tab_lock.js";
import { confirmPush, watch as watchJob } from "../progress.js";

// 欄から離れてから送るまでの間合い(ms)。連続入力の途中で送らない
const SETTLE_MS = 120;

let settleTimer = null;

/* ================================================================
   写した印 ── **誰が入れた開始時刻か**

   終了時刻を打つと、その値が次の行の開始時刻に入ります(`Same_Text`)。
   ところが写すだけで消さないので、**1行目を直の終わりの時刻に直すと、
   2行目には前の値が残ります**:

       1行目の終了 14:59 → 2行目の開始に 14:59 が入る
       1行目を 15:00 に直す → 引き継ぎは止まるが、14:59 は残ったまま

   消してよいのは**ツールが入れた値だけ**です。人が打った値は、何が
   あっても消しません。見分けるために、写した行をここに覚えておいて
   毎回サーバへ添えます ── **消すかどうかを決めるのはサーバ**
   (`logic/navigation.carry_decision`)で、こちらは「まだ誰も触って
   いない」という事実を運ぶだけです。

   人がその欄を打った時点で印を落とします。画面を開き直しても落ちます
   ── そのときは「人が打った値」として扱われ、消えません。**迷ったら
   消さないほうへ倒します。**
   ================================================================ */
const carried = new Set();

// 最後に描いた紙。**変わったら印は全部捨てます** ── 行番号は紙ごとの
// 番号なので、ページを切り替えたあとも持っていると、**別の紙の2行目**を
// ツールが入れた値だと思い込みます
let carriedSheet = "";

/*
  **打った順番。** 欄に打つたびに1つ進め、その欄に覚えます(`data-edit-at`)。
  サーバへ頼んだときの番号と比べて、**頼んだあとに打った欄は、返ってきた値で
  書き換えません。** 時の欄に「18」と打ち終えて分へ移った直後に、「1」の
  ときに頼んだ答えが返って「1」へ戻っていた(打った値が黙って消える)。
*/
let editSeq = 0;
function touched(el) { el.dataset.editAt = String(++editSeq); }
// どこまで書けたか(書けたときの `editSeq`)。これより後に打ったものが打ちかけ
let savedSeq = 0;
const unsaved = () => editSeq > savedSeq;

/*
  **保存は1本ずつ、送る瞬間の中身で。**

  自動保存は欄を離れた答えのあとに走り、そのときの中身(打ちかけの「A110」)を
  持って出ます。続けて閉じる前の保存(「A1100-O」)が出ると、2本が同時に
  サーバへ着き、**古いほうがあとに書かれて**新しい中身を上書きすることが
  ありました。保存はここで1列に並べ、中身は順番が来たときに集めます。
*/
let saveChain = Promise.resolve();
function inOrder(send) {
  const run = saveChain.then(send, send);
  saveChain = run.catch(() => {});
  return run;
}

/** 写した印を落とす(人が触った / 行を空にした)。 */
function forgetCarried(row) {
  carried.delete(Number(row));
}

// 欄には `<input>` と `<select>`(合紙・停止理由の記号)がある
function inputs() {
  return [...document.querySelectorAll("#grid-body [data-family]")];
}

/* ================================================================
   入力制限 (VBA `LOT*_KeyPress` + `MoveText` の入口側)

   **規則はサーバが決める。** どの欄に何が打てて、何文字で次へ行くかは
   `nippou/logic/input_rules.py` が持っていて、欄の属性として降りてくる:

       data-charset     digits / alnum_upper / any
       maxlength        打てる上限(短い欄だけ)
       data-advance-at  この長さまで入ったら次の欄へ
       data-next        飛び先のファミリ

   ここは属性のとおりに振る舞うだけ ── 「開始時は2桁」をここに書くと、
   同じ規則が Python と2か所に分かれる。

   **これは親切であって関門ではない。** 貼り付け・IME・古いブラウザは
   通り抜けるので、欄を離れたときのサーバ側の判定(`move_text` が数字で
   ない欄を空にする)は今までどおり効かせる。
   ================================================================ */

/**
 * 打たれた文字を、その欄の形へ直す。**弾く前に直す。**
 *
 * 全角で打たれた `Ｎ７１３１Ｔ０` を `N7131T0` にします ── IME が全角の
 * ままだった、貼り付けた元が全角だった、はどちらも現場で起きます。
 * 弾いてしまうと「打てているのに入らない」になるので、直せるものは
 * 直して受けます(サーバ側の `input_rules.normalize` と同じ考え方)。
 */
function fold(charset, text) {
  if (charset !== "alnum_upper" && charset !== "digits") return text;
  return text.normalize("NFKC");
}

/** その文字を打ってよいか。`charset` はサーバが付けた印。 */
function allowed(charset, ch) {
  if (charset === "digits") return /[0-9.\-]/.test(ch);
  if (charset === "alnum_upper") return /[0-9A-Za-z]/.test(ch);
  return true;
}

/*
  LOTNO は、**欄を離れなくても引きます。**

  以前は欄から離れたとき(blur)だけ引いていました。打っている途中に
  共有ファイルを7回読みに行かせないためです。ところが現場の打ちかたは
  「LOTNOを打って、そのまま画面を見る」で、**離れるまで何も出ません** ──
  離れないかぎり材・調質も入らないので、打ったのに空のままに見えます。

  VBA は `LOT*_Change` で、**7桁そろった時点**で引いていました。そこへ
  戻します。読みに行くのは7桁ちょうどのときだけなので、1〜6桁を打って
  いるあいだは一度も行きません(打ち終わりの1回だけ)。

  同じ番号で二度引かないよう、最後に引いたものを覚えておきます ──
  覚えないと、8桁目を打って消すたびに引き直します。
*/
const LOT_LENGTH = 7;
const lastLookedUp = new Map();     // 行 → 最後に引いたロット番号

function maybeLookup(el) {
  if (el.dataset.family !== "LOT") return false;
  const row = Number(el.dataset.row) || 0;
  const value = String(el.value || "").trim();
  if (value.length !== LOT_LENGTH) {
    // 7桁から外れたら覚えを捨てる。打ち直したら引き直せるように
    if (lastLookedUp.get(row) !== value) lastLookedUp.delete(row);
    return false;
  }
  if (lastLookedUp.get(row) === value) return false;
  lastLookedUp.set(row, value);
  clearTimeout(settleTimer);
  settleTimer = setTimeout(() => lookupLot(row), SETTLE_MS);
  return true;
}

function wireLimits(el) {
  const charset = el.dataset.charset;
  if (!charset || charset === "any") return;

  // 打った瞬間に弾く。**打ててしまってから消すと、何が悪かったのか
  // 分からない**(VBA は KeyPress で弾いていた)
  el.addEventListener("beforeinput", (event) => {
    if (!event.data) return;                 // 削除・変換確定は触らない
    // **全角で打たれたものは弾かない。** 直せる形なら通して、下の
    // `input` で半角へ直す(`fold`)── 弾くと打ち直しになる
    for (const ch of fold(charset, event.data)) {
      if (!allowed(charset, ch)) { event.preventDefault(); return; }
    }
  });

  /** 欄の中身を、その欄の形へ直す。**変換が終わってから呼ぶ。** */
  function tidy() {
    let text = fold(charset, el.value);
    if (charset === "alnum_upper") text = text.toUpperCase();
    const kept = [...text].filter((ch) => allowed(charset, ch)).join("");
    if (kept !== el.value) {
      const at = el.selectionStart;
      el.value = kept;
      try { el.setSelectionRange(at, at); } catch { /* select 等 */ }
    }
  }

  // 貼り付け・IME はここまで来る。**全角→半角と大文字へ直すのもここ**
  el.addEventListener("input", (event) => {
    // 【変換中は触らない】
    // IME で打っているあいだも `input` は飛んできます。そこで
    // `el.value` を書き換えると**変換そのものを壊します** ── 打った
    // かなが消える、候補の窓が欄から外れた場所に出たまま残る、確定
    // しても打ち直しになる。「予測変換がおかしい」と言われていたのは
    // ここでした。確定を待ってから(`compositionend`)まとめて直します。
    if (event.isComposing) return;
    touched(el);
    // **その欄は人が打った。** 写した印を落とす ── 以後、ツールは
    // この行の開始時刻を消しません
    if (el.dataset.family === "KZ" || el.dataset.family === "KH") {
      forgetCarried(el.dataset.row);
    }
    tidy();
    advance(el);
    maybeLookup(el);
  });

  // 変換が確定した。ここで初めて直す ── かな・漢字は
  // `allowed()` を通らないので、確定した瞬間に落ちます
  el.addEventListener("compositionend", () => {
    touched(el);
    tidy();
    advance(el);
    maybeLookup(el);
  });
}

/**
 * 選ぶ欄を、**触っただけで変わらない**ようにする。
 *
 * 【停止理由が勝手に入っていた】
 * 12行の表は横にも縦にも長いので、指は一日じゅうこの上を滑ります。
 * その途中に停止理由の選ぶ欄があり、
 *
 *   ・欄の上でホイールを回すと、**画面ではなく中身が動く**
 *   ・矢印キーで隣の行へ行こうとすると、開いていなくても選び直る
 *
 * ── どちらも「触ってしまった」だけで、止まってもいない分が停止として
 * 集計・グラフ・CSVまで通ります。**打っていないものが入るのが、いちばん
 * 直しにくい間違い**です(打ち間違いと違って、見直しても気づけない)。
 *
 * そこで、**開いてから選んだときだけ**変えます。ホイールは画面のほうへ
 * 通し、Esc と Delete は「(停止なし)」へ戻す道にします。
 */
function guardSelect(el) {
  if (!el.dataset.nowheel) return;

  // ホイールは画面を送るもの。**欄の中身を送るものではない**
  el.addEventListener("wheel", (event) => {
    if (document.activeElement !== el) return;    // 開いていなければ素通し
    event.preventDefault();                       // 中身は動かさない
    window.scrollBy(0, event.deltaY);             // 画面は動かす
  }, { passive: false });

  el.addEventListener("keydown", (event) => {
    // **消す道を用意する。** 間違えて入れてしまったとき、一覧を開いて
    // 先頭を探させない ── その場で Delete を押せば空に戻る
    if (event.key === "Delete" || event.key === "Backspace") {
      if (!el.value) return;
      event.preventDefault();
      el.value = "";
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
}

/**
 * 決まった長さまで入ったら、次の欄へ飛ぶ (VBA `MoveText` の `Mcount`)。
 *
 * 開始時・開始分・終了時・終了分は2文字、作業人数は1文字、停止時間は
 * 3文字。「08」「50」と打てば指を動かさずに次へ進める ── これが
 * 現場の速さの source なので、落とすと入力が目に見えて遅くなる。
 */
function advance(el) {
  const at = Number(el.dataset.advanceAt || 0);
  const next = el.dataset.next;
  if (!at || !next) return;
  if (el.value.length !== at) return;
  const target = document.getElementById(`${next}${el.dataset.row}`);
  if (!target) return;
  // **自動で進んだ時刻を覚える。** 2桁打って進んだ直後に、押し癖の Enter が
  // 来ると1つ飛ばしてしまうので、その1回だけ見送ります(`moveByEnter`)
  target.dataset.autoArrived = String(Date.now());
  target.focus();
}

/* ================================================================
   Enter で次の欄・Shift+Enter で前の欄 (v4.7.0)

   **行き先はサーバが決めます**(`logic/input_shortcuts.enter_targets` が
   `data-enter-next` / `data-enter-prev` として欄に付ける)。2桁で次へ飛ぶ
   のと同じ道を通り、行の最後の次は次の行のロット№です。

   打てない欄(読み取りのみ・使えない)は飛ばして、その先へ進みます。
   ================================================================ */
// 自動で進んだあと、この時間のうちに来た Enter は1回だけ見送る(ms)
const AUTO_ENTER_GRACE_MS = 800;

function enterStep(el, back) {
  const family = back ? el.dataset.enterPrev : el.dataset.enterNext;
  const shift = Number(back ? el.dataset.enterPrevRow : el.dataset.enterNextRow) || 0;
  if (!family) return null;
  return document.getElementById(`${family}${Number(el.dataset.row) + shift}`);
}

function moveByEnter(el, back) {
  // **押し癖の Enter で1つ飛ばさない。** 2桁で自動で進んだ直後の空の欄は、
  // 1回だけ見送る(もう1回押せば進む)
  const arrived = Number(el.dataset.autoArrived || 0);
  delete el.dataset.autoArrived;
  if (!back && !el.value && arrived && Date.now() - arrived < AUTO_ENTER_GRACE_MS) {
    return false;
  }
  let target = el;
  // 打てない欄は飛ばす。**ぐるぐる回らないよう上限を置く**
  for (let i = 0; i < 40; i += 1) {
    target = enterStep(target, back);
    if (!target) return false;
    if (!target.disabled && !target.readOnly) {
      target.focus();
      // Tab で入ったときと同じく中身を選ぶ(打てば置き換わる)
      if (target.tagName === "INPUT") target.select();
      return true;
    }
  }
  return false;
}

/* ================================================================
   入力の近道 (v4.7.0) ── **押した・離れたときだけ**値が入る

     枚数・包数のダブルクリック … 上の行と同じ(`copyAbove`)
     包数が空のまま離れた       … 1(サーバが `filled` で知らせる)
     終了の時/分の「直の終わり」 … その直の終わりの時刻(`stampShiftEnd`)

   どれも**決めるのはサーバ**(`logic/input_shortcuts`)。ここは送って、
   返ってきたとおりに塗り、入った欄を一瞬光らせるだけです。
   ================================================================ */

/** ツールが入れた欄を一瞬光らせる。**入ったことが目に見えるように。** */
function flash(row, families) {
  for (const family of families) {
    const el = document.getElementById(`${family}${row}`);
    if (!el) continue;
    el.classList.remove("is-filled");
    void el.offsetWidth;                 // もう一度光らせるため、いったん外す
    el.classList.add("is-filled");
    setTimeout(() => el.classList.remove("is-filled"), 1400);
  }
}

function paintFilled(filled) {
  if (!Array.isArray(filled)) return;
  for (const f of filled) {
    const el = document.getElementById(`${f.family}${f.row}`);
    // **入っていなければ光らせない。** 打っている欄は書き換えないので
    // (`paint`)、そのときは離れたときにもう一度入ります
    if (el && el.value === f.value) flash(f.row, [f.family]);
  }
}

/** 枚数・包数のダブルクリック ── 上の行と同じ枚数・包数。 */
async function copyAbove(input) {
  const row = Number(input.dataset.row);
  if (!row || input.readOnly || input.disabled) return;
  try {
    const body = await api.post("/api/entry/state", {
      ...collect(), row, changed: "MAI", copy_above: { row },
    });
    // 焦点のある欄も書き換える(いまダブルクリックした欄そのものなので)
    paint(body, { forceRow: row });
    const copied = body.copied;
    if (copied?.ok) {
      flash(row, Object.keys(copied.values || {}));
      toast(copied.message, "ok");
    } else if (copied?.message) {
      toast(copied.message, "warn");
    }
    if (body.weight_ask) await askWeight(body.weight_ask);
  } catch (err) {
    toastError(err);
  }
  maybeAutosave();
}

/* ---- 「直の終わり」── 終了の時/分に入っているあいだ、そのすぐ下に出す ---- */
let endChipRow = 0;
let endChipTimer = null;

function endChip() { return document.getElementById("shift-end-chip"); }

function showEndChip(input) {
  const chip = endChip();
  const text = document.getElementById("shift-end-text")?.textContent.trim();
  if (!chip || !text || input.readOnly || input.disabled) { hideEndChip(); return; }
  clearTimeout(endChipTimer);
  endChipRow = Number(input.dataset.row) || 0;
  chip.hidden = false;
  // 終了の「時」の欄の左下に置く(時・分のどちらに居ても同じ所)
  const anchor = document.getElementById(`SZ${endChipRow}`) || input;
  const at = anchor.getBoundingClientRect();
  const below = at.bottom + 3;
  const top = below + chip.offsetHeight + 8 <= window.innerHeight
    ? below : Math.max(8, at.top - chip.offsetHeight - 3);
  chip.style.left = `${Math.max(8, Math.min(at.left, window.innerWidth - chip.offsetWidth - 8))}px`;
  chip.style.top = `${top}px`;
}

function hideEndChip() {
  clearTimeout(endChipTimer);
  const chip = endChip();
  if (chip) chip.hidden = true;
  endChipRow = 0;
}

async function stampShiftEnd(row) {
  if (!row) return;
  try {
    const body = await api.post("/api/entry/state", {
      ...collect(), row, changed: "SH", stamp: { row, which: "shift_end" },
    });
    paint(body, { forceRow: row });
    if (body.stamp && !body.stamp.ok && body.stamp.message) {
      toast(body.stamp.message, "warn");
    } else if (body.stamp?.ok) {
      flash(row, ["SZ", "SH"]);
    }
    if (body.weight_ask) await askWeight(body.weight_ask);
  } catch (err) {
    toastError(err);
  }
  maybeAutosave();
}

/** 画面の入力内容をひとまとめにする。サーバへはこの形で送る。 */
function collect() {
  const rows = {};
  for (const el of inputs()) {
    const row = el.dataset.row;
    (rows[row] ||= {})[el.dataset.family] = el.value;
  }
  const header = {};
  for (const el of document.querySelectorAll("[data-header]")) {
    header[el.dataset.header] = el.value;
  }
  const checks = {};
  for (const el of document.querySelectorAll("[data-check]")) {
    checks[el.dataset.check] = el.checked;
  }
  // 理由は**行の値**。表の列には出していないので、別の欄から拾って
  // 行へ混ぜる(サーバから見れば他の欄と同じ `rows[行].reason`)
  for (const el of document.querySelectorAll("[data-reason]")) {
    (rows[el.dataset.reason] ||= {}).reason = el.value;
  }
  return {
    rows, header, checks,
    // ツールが入れた開始時刻が、まだ人に触られずに残っている行
    carried: [...carried],
    day_shift: document.getElementById("hd-day-shift")?.checked || false,
    // **この画面が自分を何の直だと思っているか。** 添えないと、開いたまま
    // 17:00 をまたいだことをサーバが見分けられません(`#sheet-key`)
    opened: openedKey(),
  };
}

/**
 * サーバが描いた「この画面の直」。**画面は自分で覚えません。**
 *
 * 覚えさせると、描かれたほうと覚えたほうが必ず食い違います ── 出どころは
 * `entry.html` の `#sheet-key` ただ1つで、`paint()` がそれも塗り替えます。
 */
function openedKey() {
  const el = document.getElementById("sheet-key");
  if (!el) return null;
  return {
    report_date: el.dataset.openedDate || "",
    shift: el.dataset.openedShift || "",
    page: Number(el.dataset.openedPage || 1),
  };
}

/**
 * サーバが返したビューモデルを画面へ写す。**判断はしない。**
 *
 * `forceRow` はその行だけ、焦点のある欄も書き換える。ロット番号から
 * 行を埋めたときに要る ── LOT を離れた指はたいてい次の欄(材・調質)に
 * 移っていて、そこを守ると**埋めたはずの値が1つだけ入らない。**
 * 打った値ではなくサーバが引いてきた値なので、上書きしてよい。
 */
function paint(view, { forceRow = 0, since = null } = {}) {
  if (!view) return;
  // **別の紙になったら、写した印は捨てる。** 行番号は紙ごとの番号です
  const sheet = `${view.report_date || ""}|${view.line || ""}|`
              + `${view.shift || ""}|${view.page || ""}`;
  if (sheet !== carriedSheet) {
    carried.clear();
    carriedSheet = sheet;
  }
  // **写した印はサーバの言うとおりに持ち直す。** 付いていない応答
  // (ロット引きなど、引き継ぎを通らない道)では触りません
  if (Array.isArray(view.carried)) {
    carried.clear();
    for (const row of view.carried) carried.add(Number(row));
  }
  for (const el of inputs()) {
    const next = view.rows?.[el.dataset.row]?.[el.dataset.family];
    if (next === undefined || next === el.value) continue;
    // 頼んだあとに打った欄は書き換えない(返ってきたのは打つ前の値の答え)
    if (since !== null && Number(el.dataset.editAt || 0) > since) continue;
    // 入力中の欄は書き換えない(カーソルが飛ぶ)
    const typing = el === document.activeElement;
    if (typing && Number(el.dataset.row) !== forceRow) continue;
    el.value = next;
  }
  for (const el of document.querySelectorAll("[data-header]")) {
    const next = view.header?.[el.dataset.header];
    if (next !== undefined && el !== document.activeElement) el.value = next;
  }
  for (const el of document.querySelectorAll("[data-check]")) {
    const next = view.checks?.[el.dataset.check];
    if (next !== undefined) el.checked = next;
  }
  const lead = document.getElementById("page-lead");
  if (lead && view.page) {
    lead.textContent = `第${view.page}ページ / 全${view.page_count || 1}ページ`;
  }
  // **帯と「この画面の直」は毎回塗り直す。** サーバが描いたきりだと、
  // 開いたまま 17:00 をまたいだときに**画面が「1直」と言いながら
  // 2直へ書く**ことになります
  paintRibbon(view.ribbon);
  paintKey(view);
  paintSheet(view);
  paintFlow(view.flow);
  paintPrevShift(view.prev_shift);
  paintReadOnly(view);
  paintReason(view);
  paintMarks(view.marks);
  paintGMarks(view.g_marks);
  paintLot(view.lot);
  // 引き継ぎ(Same_Text)が起きたら、サーバが指した欄へ移る
  if (view.focus) document.getElementById(view.focus)?.focus();
  if (view.message) toast(view.message, "ok");
  paintTimeProblem(view.time_problem);
  paintBadRows(view.bad_rows);
  paintHandover(view.handover);
  paintShiftTimesNote(view.shift_times_note);
  paintLineNote(view.line_note);
  paintNeedsWorker(view);
  paintSheets(view.sheets);
  paintShiftFindings(view.shift_findings);
  paintFilled(view.filled);
  // 「直の終わり」の時刻(**開いている直の**終わり)。直が変われば変わる
  if (typeof view.shift_end === "string") {
    const text = document.getElementById("shift-end-text");
    if (text) text.textContent = view.shift_end;
    if (!view.shift_end) hideEndChip();
  }
}

/**
 * 紙の束 ── **VBA の シートタブ に当たるもの。**
 *
 *     直終わりに保存が済み　次直が登録すると前直分はどうなりますか？
 *     消えるのですか？
 *
 * 消えません。並べるのは、**消えていないことを目で見せる**ためです ──
 * VBA はシートのタブが並んでいたので、発行すれば束に1枚増えて、
 * そこへ移ったことも、前の紙が残っていることも読めました。
 *
 * **並びも札の状態もサーバが決めます**(`logic/sheet_strip.py`)。
 * ここは写して、押されたら開くだけ。応答のたびに組み直すので、
 * 保存・次ページ発行・直の切り替わりが、そのまま束に出ます。
 */
function paintSheets(strip) {
  const box = document.getElementById("sheets");
  const row = document.getElementById("sheets-row");
  if (!box || !row) return;
  const sheets = strip?.sheets || [];
  box.hidden = !sheets.length;
  // **鍵も塗り直す。** 日付とラインは束が自分で持ちます ── 隣から
  // 拾うと、まだ入っていない時点で押されて空の紙が開きます
  if (strip?.report_date) box.dataset.sheetsDate = strip.report_date;
  if (strip?.line) box.dataset.sheetsLine = strip.line;
  const note = document.getElementById("sheets-note");
  if (note) note.textContent = strip?.summary || "";

  // **作り直します。** 札は多くて9枚(3直×3ページ)なので、差分を取る
  // よりまるごと組むほうが短く、食い違いも起きません
  row.textContent = "";
  for (const sheet of sheets) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "sheets__tab" + (sheet.now ? " is-now" : "");
    tab.setAttribute("role", "listitem");
    tab.dataset.sheetShift = sheet.shift;
    tab.dataset.sheetPage = String(sheet.page);
    tab.dataset.sheetOpen = sheet.open_as || "";
    tab.title = sheet.note || "";
    tab.setAttribute("aria-label", sheet.note || sheet.label || "");
    if (sheet.now) tab.setAttribute("aria-current", "true");

    const label = document.createElement("b");
    label.className = "sheets__label";
    label.textContent = sheet.label || "";
    const mark = document.createElement("span");
    mark.className = `sheets__mark sheets__mark--${sheet.state || ""}`;
    mark.textContent = sheet.mark || "";
    tab.append(label, mark);
    row.append(tab);
  }
}

/**
 * 直の時間の出どころ ── **控えで動いているなら、そう言う。**
 *
 * 決めるのはサーバ(`logic/shift.shift_times_note`)。取り込めば消えます。
 */
function paintShiftTimesNote(text) {
  const box = document.getElementById("shift-times-note");
  if (!box) return;
  box.textContent = text || "";
  box.hidden = !text;
}

/**
 * この端末のラインを決めていない関門(`work_context.terminal_line_note`)。
 * 表を伏せるのは `paintNeedsWorker`(`needs_line`)、断るのはサーバです。
 */
function paintLineNote(text) {
  const box = document.getElementById("terminal-line-note");
  if (!box) return;
  const words = document.getElementById("terminal-line-text");
  if (words) words.textContent = text || "";
  box.hidden = !text;
}

/**
 * 前の直の始末 ── **押し忘れのまま次の直が進むのを止める関門。**
 *
 * 決めるのはサーバ(`logic/handover.decide`)。ここは出すだけです。
 *
 * **止まるのと、知らせるのは別です。** `blocked` は「この直はまだ
 * 1ページも打っていない」ときだけ立ちます ── 打ち始めた人を途中で
 * 止めないためです。`any_left` のほうは共有へ出るまで立ったままなので、
 * 打ち始めたあとも帯は残ります。
 */
function paintHandover(handover) {
  const box = document.getElementById("handover");
  if (!box) return;
  const left = !!handover?.any_left;
  box.hidden = !left;
  document.body.classList.toggle("handover-blocked", !!handover?.blocked);
  if (!left) return;

  // **主語。** 「前の直 9月12日 1直 のこと」── 下の帯(いまの直)と
  // 言い合っているように読まれないための札です(決めるのはサーバ)
  const about = document.getElementById("handover-about");
  if (about) about.textContent = handover.about || "";

  const text = document.getElementById("handover-text");
  if (text) text.textContent = handover.message || "";

  const list = document.getElementById("handover-list");
  if (list) {
    list.replaceChildren();
    for (const line of handover.problems || []) {
      const li = document.createElement("li");
      li.textContent = line;
      list.append(li);
    }
  }
  // 引き継いだあとに残る一行。**片付いたことにはしません**
  const notes = document.getElementById("handover-notes");
  if (notes) {
    notes.replaceChildren();
    for (const line of handover.notes || []) {
      const li = document.createElement("li");
      li.textContent = line;
      notes.append(li);
    }
  }
  const take = document.getElementById("handover-take");
  if (take) take.hidden = !(handover.actions || []).includes("take");
}

/**
 * 直の始まり ── **まず作業者を選ぶ。**
 *
 * 決めるのはサーバ(`view.needs_worker`)。作業者が空のあいだは12行の表と
 * 保存の並びを伏せ、作業者のカードだけを残します。
 *
 * **消すのではなく伏せます。** 消すと「何も無い画面」に見えますが、
 * 伏せてあるだけなら「まだ触れない」と読めます。
 *
 * 選んだ時点で `settle` がサーバへ行き、`needs_worker` が下りて
 * ここが呼ばれ直します ── 画面側で解除を判断しません。
 */
function paintNeedsWorker(view) {
  // **伏せる理由は2つあります。** 作業者がまだ決まっていない(こちら)と、
  // 前の直が片付いていない(`handover.blocked`)── どちらでも表は伏せ
  // ますが、出す帯と押させるボタンが違います
  //
  // **3つめはラインが決まっていない(`needs_line`、v4.3.0)。** いちばん
  // 先です ── どのラインの日報かが決まらないと、作業者も前の直も
  // 意味を持ちません。このときは作業者のカードまで伏せます
  const noLine = !!view?.needs_line;
  const blocked = !noLine && !!view?.handover?.blocked;
  const need = noLine || !!view?.needs_worker || blocked;
  const box = document.getElementById("start-shift");
  // 前の直が先。**2つの帯を同時に出さない** ── どちらから触るのかが
  // また分からなくなります
  if (box) box.hidden = noLine || !view?.needs_worker || blocked;
  const now = box?.querySelector(".start-shift__now");
  if (now && view?.report_date) {
    now.textContent = `${view.report_date} ${view.shift}`;
  }
  document.body.classList.toggle("needs-worker", need && !blocked && !noLine);
  document.body.classList.toggle("needs-line", noLine);
  // 表と保存の並びだけ止める。**作業者のカードと帯は残す** ── そこを
  // 触ってほしいので
  //
  // 表の中だけでは足りませんでした。**全停入力と次ページ発行は、押した
  // 時点で1ページを書いて保存します** ── 表が伏せてあっても、この2つは
  // 押せてしまい、「誰の直か分からない紙」が1枚できます
  // (「作業者を選ぶ前に全停入力できてしまう」)。印は HTML 側の
  // `data-need-worker` で、断るのはサーバです。
  const grid = document.getElementById("grid-body");
  // 作業者のカードは**ラインが決まっていないときだけ**伏せる(作業者を
  // 選ばせる帯では、そこを触ってほしいので開けておく)
  const card = document.querySelector('[data-step-for="worker"]');
  const locked = [
    ...[...(grid ? grid.querySelectorAll("input, select, textarea, button") : []),
        ...document.querySelectorAll("[data-need-worker]")].map((el) => [el, need]),
    ...[...(card ? card.querySelectorAll("input, select, textarea, button") : [])]
      .map((el) => [el, noLine]),
  ];
  const why = noLine
    ? "先に「ラインを決める」で、この端末のラインを決めてください"
    : "先に「作業者を選ぶ」でこの直の作業者を決めてください";
  for (const [el, lock] of locked) {
    const named = el.dataset.needWorker !== undefined;
    if (lock) {
      // **自分で止めたものだけ印を付ける。** 既に止まっているもの
      // (呼出中の「次ページ発行」)に印を付けると、作業者が決まった
      // ときにこちらが勝手に押せるようにしてしまいます
      if (el.disabled) continue;
      el.dataset.needWorkerLocked = "1";
      el.disabled = true;
      if (named) {
        el.dataset.needWorkerTitle = el.title || "";
        el.title = why;
      }
    } else if (el.dataset.needWorkerLocked) {
      delete el.dataset.needWorkerLocked;
      el.disabled = false;
      if (named) {
        el.title = el.dataset.needWorkerTitle || "";
        delete el.dataset.needWorkerTitle;
      }
    }
  }
}

/**
 * 「この画面の直」を、サーバが返した保存先に合わせる。
 *
 * **書いた先をそのまま写す。** 直の変わり目で「打っていた直へ入れる」を
 * 選んだときは、サーバはそちらへ書いて、そちらを返してきます ── ここで
 * 時計の直に戻してしまうと、次の保存でまた同じことを訊かれます。
 */
function paintKey(view) {
  const el = document.getElementById("sheet-key");
  if (!el || !view.report_date || !view.shift) return;
  el.dataset.openedDate = view.report_date;
  el.dataset.openedShift = view.shift;
  el.dataset.openedPage = String(view.page || 1);
  // ライン も持たせる ── 断りの中の「その直を開く」が、**同じ直の
  // ページ移動**(誰でも)と**他の直の呼び出し**(管理者)のどちらかを
  // 選ぶのに要ります(`openFinding`)
  el.dataset.openedLine = view.line || "";
  // **塗るのは「いまどこへ書いているか」だけ。** 題は動かないので触らない
  // ── `textContent` でまるごと書き換えると、大きく出している組み方
  // (`.sheet-key__now`)が消えて1行の地の文に戻ります
  const now = el.querySelector(".sheet-key__now");
  // 見せる字は `line_label`(ラインが決まっていなければ「未設定」。L1 と出さない)
  const text = `${view.report_date} ${view.shift} ／ ${view.line_label || view.line || ""}`;
  if (now) now.textContent = text;
  else el.textContent = text;
}

/**
 * 紙1枚の埋まりぐあいと、重量のトン表記。
 *
 * **紙は12行しかない。** 残りが読めないと、13行目を打とうとして手が
 * 止まる ── 数え方も文言もサーバが決める(`view.used_rows` ほか)。
 */
function paintSheet(view) {
  const left = document.getElementById("rows-left");
  const guide = view.page_guide;
  if (left && guide) {
    // **一言はサーバのもの**(`page_guide.rows_text`)。12行目が終了まで
    // 入ったか・前のページを直しているかで変わる
    left.textContent = guide.rows_text || "";
    left.dataset.mode = guide.mode || "";
    if (view.sheet_full) left.dataset.full = "1"; else delete left.dataset.full;
  }
  paintPageGuide(guide);
  // 使い切ったら「新しいページ」を次の一手として立てる
  const next = document.getElementById("new-page");
  if (next) next.classList.toggle("btn--primary", !!view.sheet_full);

  const tons = document.getElementById("weight-t");
  if (tons) tons.textContent = view.weight_t ? `${view.weight_t} T` : "";
}

/**
 * ページの案内(v4.8.0 → v4.9.0)── 表のすぐ上の**1行**。
 *
 *     12行目まで済んだ … 「次ページ発行」で第N+1ページへ・引き継ぐもの [ここへ]
 *     赤い行がある     … 先に赤い行を直す
 *     出した直後       … 前のページの直し方                         [ページへ]
 *     前のページを直す … 直して「保存(確定)」、続きは「最新のページに戻る」
 *
 * **出すかどうかも中身もサーバが決める**(`presenters/entry.page_guide`)。
 * くわしくはマウスを乗せると出ます(`data-hint` ← `detail`)。
 */
const GUIDE_SHOWN = new Set(["ready", "fix", "issued", "editing"]);

function paintPageGuide(guide) {
  const box = document.getElementById("page-guide");
  if (!box || !guide) return;
  const shown = GUIDE_SHOWN.has(guide.mode);
  box.hidden = !shown;
  box.dataset.mode = guide.mode || "";
  if (!shown) return;
  const text = document.getElementById("page-guide-text");
  if (text) {
    text.textContent = guide.text || "";
    text.dataset.hint = guide.detail || "";
  }
  const buttons = { "goto-new-page": "page-guide-goto", "goto-pages": "page-guide-pages",
                    back: "page-guide-back" };
  for (const [action, id] of Object.entries(buttons)) {
    const btn = document.getElementById(id);
    if (btn) btn.hidden = guide.action !== action;
  }
}

/**
 * 直ひと回りの動線。**済み・いまどこか・次の一言を決めるのはサーバ**
 * (`presenters/flow.py`)。ここは写すだけ。
 */
function paintFlow(flow) {
  if (!flow) return;
  const head = document.querySelector(".flow__head");
  if (head) head.textContent = flow.headline || "";
  for (const item of flow.steps || []) {
    const el = document.querySelector(`#steps [data-step="${item.key}"]`);
    if (!el) continue;
    if (item.done) el.dataset.done = "1"; else delete el.dataset.done;
    if (item.current) {
      el.dataset.current = "1";
      el.setAttribute("aria-current", "step");
    } else {
      delete el.dataset.current;
      el.removeAttribute("aria-current");
    }
    // 件数(「直すところが2件」「未保存 2ページ」)は打つたびに変わる
    const note = el.querySelector(".step__text small");
    if (note) note.textContent = item.note || "";
    // **いまやることにだけ**、そこへ行くものを付ける。
    //   別の画面 → リンク / この画面 → そのボタンまで運ぶ「ここへ」
    // (押したときの動きは `app.js` が document で受けている)
    el.querySelector(".step__go")?.remove();
    if (!item.current) continue;
    if (item.url && item.url !== "/") {
      const a = document.createElement("a");
      a.className = "btn btn--sm btn--primary step__go";
      a.href = item.url;
      a.textContent = item.action || "開く";
      el.appendChild(a);
    } else if (item.focus) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn--sm btn--primary step__go";
      btn.dataset.goto = item.focus;
      btn.textContent = "ここへ";
      el.appendChild(btn);
    }
  }
}

/**
 * 前の直に1ページも無い、の帯。**出すかどうかを決めるのはサーバ**
 * (`logic/prev_shift.py`)。一度「分かりました」を押せば、その直では
 * もう出ません(覚えるのもサーバ)。
 */
function paintPrevShift(state) {
  const box = document.getElementById("prev-shift");
  if (!box || !state) return;
  box.hidden = !state.warn;
  if (!state.warn) return;
  const message = document.getElementById("prev-shift-message");
  const reason = document.getElementById("prev-shift-reason");
  if (message) message.textContent = state.message || "";
  if (reason) reason.textContent = state.reason || "";
}

/**
 * 見るだけの帯と、打てなくする処理。**決めるのはサーバ**(`view.read_only`)。
 *
 * 他の直の過去データを、管理者モードでないまま開いている状態です。
 * 保存は前から 403 で断っていましたが、**打つことはできました** ──
 * 断られるのは押したときなので、それまでは直せているつもりで手を
 * 動かします(「管理者モード終わった後でも1直データが開いてたら
 * いじれるのはNG」)。
 *
 * 押し間違いだけでこうなるのではありません。管理者が過去の直を開いた
 * まま直の変わり目をまたぐと、管理者モードは自動で外れ、呼出モードは
 * わざと残ります ── **誰も何も操作していないのに**この状態になります。
 *
 * 触れなくするのは `#main` の中の、この帯より下だけ。帯そのもの(出口の
 * 2つ)と、左の並び・帯の「終了」は残します。
 */
function paintReadOnly(view) {
  const box = document.getElementById("read-only");
  if (!box) return;
  const only = !!view?.read_only;
  box.hidden = !only;
  const what = document.getElementById("read-only-what");
  if (what && view?.report_date) {
    what.textContent = `${view.report_date} ${view.shift} 第${view.page}ページ`;
  }
  applyReadOnly(only);
}

/**
 * 見るだけの状態を、画面に効かせる。
 *
 * **描かれた直後にも呼びます。** 帯そのものはサーバが描いています
 * (`entry.html` の `hidden`)が、触れなくするのは JS の仕事なので、
 * `paint()` を待つと**最初の1回だけ打ててしまいます** ── 過去データを
 * 開いたまま画面を開き直した人が、まさにその1回に当たります。
 */
function applyReadOnly(only) {
  document.body.classList.toggle("is-readonly", only);
  const scope = document.getElementById("main");
  if (!scope) return;
  for (const el of scope.querySelectorAll("input, select, textarea, button")) {
    if (el.closest("#read-only")) continue;
    if (only) {
      if (!el.disabled) el.dataset.readOnlyLocked = "1";
      el.disabled = true;
    } else if (el.dataset.readOnlyLocked) {
      delete el.dataset.readOnlyLocked;
      el.disabled = false;
    }
  }
}

/*
  理由 (紙の「ヨ：その他（理由を記載）」)

  **いつでも書く欄ではない。** 作業停止の記号で「その他」を選んだとき
  だけ開く ── どの記号が「その他」かを決めるのはサーバ
  (`presenters/entry.py:other_stop_codes`)で、ここは `reason_open` を
  写すだけ。記号の一覧をJSへ持ってくると、マスタを差し替えた日に
  ここだけ古いまま残る。

  **開いた瞬間にポップアップを出す。** 選んだその場で書けるので、画面の
  上へ戻って空欄を探さずに済む。閉じても入力は消えず、「理由を書く…」で
  開き直せる。
*/
let reasonAsked = "";

/**
 * 理由の欄を、**行ごとに**並べ直す。
 *
 * 紙の「ヨ：その他（理由を記載）」は欄が1つしかなく、VBA もそれに
 * 合わせてページに1つでした。**紙の都合であって、書きたいことの都合では
 * ありません** ── 停止理由で「その他」を選ぶのは行ごとなので、理由も
 * 行ごとに持ちます(紙と共有へ出すときだけ1つにまとめます)。
 *
 * **どの行に欄が要るかを決めるのはサーバ**(`presenters/entry.
 * reason_entries`)です。ここは受け取った並びをそのまま写します。
 */
function paintReason(view) {
  const host = document.getElementById("reason-rows");
  const entries = view.reason_entries;
  if (!host || entries === undefined) return;

  const typing = document.activeElement;
  const keep = typing && typing.dataset && typing.dataset.reason;

  // いま打っている欄は作り直さない(カーソルが飛ぶ)
  const have = new Map([...host.querySelectorAll("[data-reason-row]")]
    .map((el) => [el.dataset.reasonRow, el]));
  const wanted = new Set(entries.map((e) => String(e.row)));
  for (const [row, el] of have) {
    if (!wanted.has(row)) el.remove();
  }

  for (const entry of entries) {
    const row = String(entry.row);
    let box = have.get(row);
    if (!box) {
      box = document.createElement("div");
      box.className = "reason__row";
      box.dataset.reasonRow = row;
      const label = document.createElement("label");
      label.htmlFor = `reason-${row}`;
      label.textContent = entry.label;
      const input = document.createElement("input");
      input.type = "text";
      input.id = `reason-${row}`;
      input.dataset.reason = row;
      input.size = 44;
      input.placeholder = "例: 棚卸し準備、点検表の差し替え";
      input.addEventListener("blur", () => settle());
      box.append(label, input);
      // 行の順に差し込む(並べ替えではなく、入れる場所を選ぶ)
      const after = [...host.querySelectorAll("[data-reason-row]")]
        .find((el) => Number(el.dataset.reasonRow) > entry.row);
      host.insertBefore(box, after || null);
    }
    const input = box.querySelector("input");
    if (input && input !== keep && input.value !== entry.text) {
      input.value = entry.text;
    }
    if (entry.empty) box.dataset.empty = "1"; else delete box.dataset.empty;
    let lot = box.querySelector(".lead");
    if (entry.lot) {
      if (!lot) { lot = document.createElement("span"); lot.className = "lead"; box.append(lot); }
      lot.textContent = entry.lot;
    } else if (lot) {
      lot.remove();
    }
  }

  const none = document.getElementById("reason-empty");
  if (none) none.hidden = entries.length > 0;

  // 「その他」を選んだのに空の行があれば、そこへ促す ── **止めません**
  const note = document.getElementById("reason-note");
  const empty = entries.filter((e) => e.empty).map((e) => e.label);
  if (note) {
    note.innerHTML = empty.length
      ? `<b>${empty.join("、")}</b>の理由がまだ空です。何があったのかを書いてください。`
      : '作業停止の記号で<b>「その他」</b>を選ぶと、<b>その行ごと</b>に書けます。';
  }

  // **開いた瞬間だけ**ポップアップを出す。出し続けると、欄を離れる
  // たびに割り込んで入力が進まない
  const first = entries.find((e) => e.empty);
  const seen = first ? String(first.row) : "";
  if (first && seen !== reasonAsked) askReason(first);
  reasonAsked = seen;
}

function askReason(entry) {
  const text = document.getElementById("reason-text");
  const why = document.getElementById("reason-why");
  if (!text || !entry) return;
  text.dataset.row = String(entry.row);
  text.value = document.getElementById(`reason-${entry.row}`)?.value || "";
  if (why) {
    why.textContent =
      `${entry.label}の作業停止で「その他」を選んでいます。`;
  }
  openModal("reason-modal");
  text.focus();
  text.select();
}

/*
  時間計算の断り (VBA `時間計算` の MsgBox)

  **VBA は MsgBox で止めていたが、ここは止めない。** 打っている途中で
  ダイアログが出ると、続きが打てなくなる ── 12行を続けて打つ現場では
  それだけで手が止まります。代わりに、その行に印を付けて文言を出します。

  作業時間の欄は**サーバが空のまま返します**(VBA も `Exit Sub` して
  何も書かなかった)。だから「直さないと時間が出ない」ことが画面で
  分かり、断りを見落としても計算だけが通ることはありません。
*/
let lastProblem = "";

/**
 * 直っていない行の印。**消えない印。**
 *
 * 断りの文言はトーストと帯に出ますが、どちらも数秒で消えるか、画面を
 * スクロールすると目に入らなくなります ── それで「警告は出ているのに
 * 入力が進められてしまう」状態になっていました。行そのものに印を残し、
 * 保存(確定)も断ります(`/api/entry/save`)。
 *
 * 何行目が悪いかを決めるのは**サーバ**(`logic/work_time.problems`)です。
 */
function paintBadRows(rows) {
  const bad = new Map((rows || []).map((b) => [String(b.row), b]));
  for (const tr of document.querySelectorAll("#grid-body tr")) {
    const found = bad.get(tr.dataset.row);
    if (found) {
      tr.dataset.bad = "1";
      tr.title = found.message;
    } else {
      delete tr.dataset.bad;
      tr.removeAttribute("title");
    }
  }
  // 直すところが何行あるか。**保存を押す前に見えるところへ**
  const note = document.getElementById("bad-note");
  if (note) {
    note.hidden = bad.size === 0;
    note.textContent = bad.size
      ? `直すところが ${bad.size}行 あります。直すまで保存(確定)できません。`
      : "";
  }
  const save = document.getElementById("save");
  if (save) save.classList.toggle("btn--blocked", bad.size > 0);
}

function paintTimeProblem(problem) {
  for (const tr of document.querySelectorAll("#grid-body tr")) {
    delete tr.dataset.problem;
  }
  const box = document.getElementById("time-note");
  if (!problem) {
    lastProblem = "";
    if (box) { box.hidden = true; box.textContent = ""; }
    return;
  }

  const tr = document.querySelector(`#grid-body tr[data-row="${problem.row}"]`);
  if (tr) tr.dataset.problem = "1";
  if (box) {
    box.hidden = false;
    box.textContent = problem.message;
    box.className = "msg msg--warn";
  }

  // **同じ断りで鳴らし続けない。** 欄を離れるたびに送るので、押さえないと
  // 直すまで鳴り続ける(サーバ側の `SoundGate` は1分ごとの判断用)
  const seen = `${problem.reason}:${problem.row}`;
  if (problem.sound && seen !== lastProblem) {
    playKey("negative_time").catch(() => { /* 鳴らなくても文言は出ている */ });
  }
  lastProblem = seen;
}

/* ================================================================
   etc欄の押しボタン (VBA SPCommand〜MICommand + CommandLook_For)

   **押した状態は持たない。** 値は etc 欄の文字そのもので、行を移る
   たびにサーバがそこから読み直した結果(`view.marks`)を写すだけ。
   持たせると、過去データを開いたときやページを移ったときに食い違う。
   ================================================================ */
let markRow = 0;      // いまボタンが効く行(VBA の `UFdaily.RowCount`)
let lastMarks = null; // サーバが最後に返した行ごとの印。行を移るとき塗り直す

function paintMarks(marks) {
  if (marks) lastMarks = marks;
  const label = document.getElementById("marks-row");
  if (label) label.textContent = markRow ? String(markRow) : "—";
  const active = new Set((lastMarks && lastMarks[String(markRow)]) || []);
  for (const btn of document.querySelectorAll("[data-mark]")) {
    const on = markRow && active.has(btn.dataset.mark);
    btn.classList.toggle("btn--primary", !!on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    /*
      【押せなくするのをやめました ── 「2回押して効く」の正体】
      以前ここは、行を選んでいないあいだ `btn.disabled = true` に
      していました。**無効のボタンは click を出しません。** つまり
      押しても、toast も出なければ音もしない ── 何も起きません。

          1度目 … 押す → 何も起きない(無効だった)
          その後 … 表のどこかを触る → 行が決まってボタンが生きる
          2度目 … 押す → 効く

      押した人からは「2回押さないと効かない」に見えます。原因が
      画面に出ないので、なおさら分かりません。

      いまは**いつでも押せます。** 行が決まっていなければ、押したときに
      決めます(`rowForMarks`)── どの行に効いたかは、すぐ左の
      「いま N 行目」に出ます。
    */
    btn.disabled = false;
  }
  const note = document.getElementById("marks-note");
  if (note) {
    note.textContent = markRow
      ? "" : "押すと、打ちかけの行(無ければ1行目)に付きます";
  }
}

/**
 * ボタンを押したとき、どの行に効かせるか。
 *
 * 選んでいればその行。選んでいなければ**打ちかけの行**(何か入っている
 * いちばん下の行)、それも無ければ1行目。勝手に決めますが、決めた行は
 * 「いま N 行目」に出るので、どこに付いたかは見えます。
 */
function rowForMarks() {
  if (markRow) return markRow;
  let last = 0;
  for (const el of inputs()) {
    if (el.dataset.family === "ET") continue;   // etc欄そのものは数えない
    if (String(el.value || "").trim()) {
      last = Math.max(last, Number(el.dataset.row) || 0);
    }
  }
  return last || 1;
}

/**
 * Gコースのロットの印(寸法の欄の「G」── v4.17.0)。**どの行に付けるかはサーバ**
 * (`presenters/entry.g_marks`)。付いていない応答では触らない。
 */
function paintGMarks(marks) {
  if (!marks) return;
  for (const el of document.querySelectorAll("[data-gmark-row]")) {
    const mark = marks[el.dataset.gmarkRow];
    el.hidden = !mark;
    el.textContent = mark ? mark.text : "G";
    el.title = mark ? mark.title : "";
    if (mark) el.setAttribute("aria-label", mark.title);
    else el.removeAttribute("aria-label");
  }
}

/*
  ロット番号を打ったあと (VBA `SQLiteLot検索`)

  サーバが3つのファイルを辿った結果だけを写す。**引けなかったことは
  画面に出すが、打ったロット番号は消さない** ── そのあと手で埋められる。
*/
function paintLot(lot) {
  if (!lot) return;
  const cell = document.getElementById(`LOT${lot.row}`);

  if (lot.state === "choose") {
    askAllocation(lot);
    return;
  }
  if (lot.state === "filled") {
    // **選んだ引当をマウスで確かめられるようにする。** 埋まった値が
    // どの受注のものかは、画面のどこにも出ていない
    if (cell) {
      cell.title = `受注 ${lot.order_no}`
        + (lot.hiki_no ? ` / 引当 ${lot.hiki_no}` : "")
        + (lot.auto_marks && lot.auto_marks.length
           ? `\n自動: ${lot.auto_marks.join("、")}` : "");
      cell.dataset.filled = "1";
    }
    if (lot.message) toast(lot.message, "ok");
    // Gコースのロット。**寸法が SIKALOT の製造の寸法と違う**ので、引いたときに言う
    if (lot.g_course) toast(lot.g_course.message, "info");
    showPackNote(lot);
    return;
  }
  if (lot.state === "short") return;      // 7桁そろうまでは黙って待つ

  // 引けなかった。理由を出して、手入力に任せる
  if (cell) { delete cell.dataset.filled; cell.title = lot.message || ""; }
  if (lot.message) toast(lot.message, "warn");
}

/*
  包装仕様の注意 (VBA `PackagingSpecificationNo` → `MsgBox`)

  **コイルだけ目の前に出す。** VBA は `If arr(0) <> "板" Then MsgBox`
  ── 板は検査側で出るので、日報では etc 欄へ入れるだけでした。
  出す・出さないの判断はサーバ(`is_coil`)で、ここは写すだけ。
*/
function showPackNote(lot) {
  const note = lot.note;
  if (!note || !note.has_note) return;

  if (!note.is_coil) {
    // 板。**帯に出して流す** ── 手を止めさせるほどではないが、
    // 気づかないまま進むのも困る
    toast(note.message, "warn");
    return;
  }

  const where = document.getElementById("note-where");
  if (where) {
    where.textContent =
      `${lot.row}行目 ロット ${lot.lot_no} / 包装仕様 ${note.pack_spec_no}`
      + `(${note.shape})`;
  }
  const list = document.getElementById("note-list");
  if (list) {
    list.replaceChildren();
    for (const text of note.comments || []) {
      const item = document.createElement("li");
      item.textContent = text;
      list.append(item);
    }
  }
  openModal("note-modal");
}

/* 引当が複数あるとき。**選ぶまで何も書き換えない** */
let pickedHiki = "";

function askAllocation(lot) {
  const host = document.getElementById("hiki-rows");
  if (!host) return;
  pickedHiki = "";
  host.replaceChildren();
  for (const a of lot.choices || []) {
    const tr = document.createElement("tr");
    const pick = document.createElement("td");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "hiki-pick";
    radio.value = a.hiki_no;
    radio.addEventListener("change", () => { pickedHiki = a.hiki_no; });
    pick.appendChild(radio);
    tr.appendChild(pick);
    for (const text of [a.hiki_no, a.order_no, a.quantity || "全量",
                        a.adjust_no || ""]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    // 行のどこを押しても選べる(小さい丸を狙わせない)
    tr.addEventListener("click", () => { radio.checked = true; pickedHiki = a.hiki_no; });
    host.appendChild(tr);
  }
  const why = document.getElementById("hiki-why");
  if (why) {
    why.textContent =
      `ロット ${lot.lot_no} には引当が${(lot.choices || []).length}件あります。`
      + "選んだ引当で ＶＣ・単重・合紙・EX が決まります。";
  }
  const apply = document.getElementById("hiki-apply");
  if (apply) apply.dataset.row = String(lot.row);
  openModal("hiki-modal");
}

/** ロット番号でサーバに引かせる。`hiki_no` を添えるとその引当で埋める。 */
async function lookupLot(row, hikiNo = "") {
  try {
    const since = editSeq;
    const body = await api.post("/api/entry/lot",
                                { ...collect(), row, hiki_no: hikiNo });
    // **その行は焦点があっても書き換える。** LOT を離れた指は次の欄に
    // 移っているので、守ると材・調質だけが入らない(引いているあいだに
    // 打った欄だけは、打った値のまま)
    paint(body, { forceRow: row, since });
  } catch (err) {
    toastError(err);
  }
}

/** 欄から離れたときに、決まる値を決めてもらう。 */
async function settle(row, changed = "") {
  try {
    const since = editSeq;
    const body = await api.post("/api/entry/state",
                                { ...collect(), row, changed });
    paint(body, { since });
    // **重量を計算し直してよいか聞く。**
    //
    // VBA は重量が入っている行を触りませんでした(現物を量った値のほうが
    // 正しいため)。そのぶん、枚数を打ち直しても重量は前のまま ──
    // 食い違ったまま保存できます。黙って上書きも、黙って据え置きも
    // 重すぎるので、ここで聞きます。聞くかどうかを決めたのはサーバで
    // (`calculations.weight_needs_asking`)、ここは出して返すだけ。
    if (body.weight_ask) await askWeight(body.weight_ask);
  } catch (err) {
    toastError(err);
  }
  maybeAutosave();
}

/**
 * その行を空にする(✕)。
 *
 * **1行ずつ手で消すのが手間**でした ── 12個の欄を1つずつ選んで消して
 * 回ることになります。打ち間違えた行を消すだけなのに。
 *
 * **下の行は繰り上がりません。** 紙は12行の罫線が引かれた用紙で、
 * 行番号は紙の行番号です。繰り上げると、紙と画面で行がずれます。
 *
 * 消したあとは `settle` を通します ── 合計も作業時間も、消したぶんを
 * 引いて出し直す必要があるので。
 */
/**
 * 開始・終了の時/分をダブルクリック ── **いまの時刻を時と分の2欄へ**。
 *
 *     普通に入力も可能だが、ダブルクリックで2つのテキストボックスを
 *     簡単に埋めれる
 *
 * 時刻を決めるのは**サーバ**です(分の1の位を 0 か 5 に寄せる規則も
 * `logic/work_time.stamp`)。ここは「どの行の開始/終了か」を添えて、
 * 打ったときと同じ道(`/api/entry/state`)を通すだけ ── 作業時間の計算・
 * 次の行への引き継ぎも、打ったときと同じに走ります。
 *
 * 見るだけの画面(読み取りのみ・使えない欄)では何もしません。
 */
async function stampTime(input) {
  const which = input.dataset.stamp;
  const row = Number(input.dataset.row);
  if (!which || !row || input.readOnly || input.disabled) return;
  // **人が入れた値**として扱う(ツールが写した開始時刻の印は落とす)
  if (which === "start") forgetCarried(row);
  try {
    const body = await api.post("/api/entry/state", {
      ...collect(), row, changed: which === "start" ? "KH" : "SH",
      stamp: { row, which },
    });
    // 焦点のある欄も書き換える(いまダブルクリックした欄そのものなので)
    paint(body, { forceRow: row });
    if (body.stamp && !body.stamp.ok && body.stamp.message) {
      toast(body.stamp.message, "warn");
    }
    if (body.weight_ask) await askWeight(body.weight_ask);
  } catch (err) {
    toastError(err);
  }
  maybeAutosave();
}

/*
  ---- 行を空にする (✕) ----

  確かめは**押した ✕ のすぐ下**に出す(`#row-ask`)。ブラウザの確かめ
  (`confirm`)は画面のいちばん上に出るので、✕ から遠く、毎回マウスを
  運ぶ手間がありました。「空にする」が ✕ のすぐ下にあるので、続けて押すだけ。
  Esc・ほかの所を押す・表や画面を動かすと、何もせずに閉じます。
*/
let askingRow = 0;

function askClearRow(btn) {
  const row = Number(btn.dataset.clearRow);
  const tr = document.querySelector(`#grid-body tr[data-row="${row}"]`);
  const box = document.getElementById("row-ask");
  if (!tr || !box) return;
  const filled = [...tr.querySelectorAll("[data-family]")]
    .some((el) => String(el.value || "").trim());
  // **空の行は黙って何もしない。** 確かめを出しても答えは1つしかない
  if (!filled) { closeRowAsk(); return; }
  askingRow = row;
  // どの行かは番号と LOT で言う(行そのものも塗る ── CSS の aria-expanded)
  const lot = String(tr.querySelector('[data-family="LOT"]')?.value || "").trim();
  document.getElementById("row-ask-text").textContent =
    `${row}行目${lot ? `(${lot})` : ""}を空にしますか?`;
  box.hidden = false;
  // ✕ のすぐ下に置く ── 行の中身(LOT など)を隠さない。下に入らなければ上へ
  const at = btn.getBoundingClientRect();
  const left = Math.min(at.left - 2, window.innerWidth - box.offsetWidth - 8);
  const below = at.bottom + 4;
  const top = below + box.offsetHeight + 8 <= window.innerHeight
    ? below : Math.max(8, at.top - box.offsetHeight - 4);
  box.style.left = `${Math.max(8, left)}px`;
  box.style.top = `${top}px`;
  for (const other of document.querySelectorAll("[data-clear-row][aria-expanded]")) {
    other.removeAttribute("aria-expanded");
  }
  btn.setAttribute("aria-expanded", "true");
  document.getElementById("row-ask-yes").focus();
}

function closeRowAsk({ refocus = false } = {}) {
  const box = document.getElementById("row-ask");
  if (!box || box.hidden) return;
  box.hidden = true;
  const btn = document.querySelector(`[data-clear-row="${askingRow}"]`);
  btn?.removeAttribute("aria-expanded");
  if (refocus) btn?.focus();
  askingRow = 0;
}

async function clearRow(row) {
  const tr = document.querySelector(`#grid-body tr[data-row="${row}"]`);
  if (!tr) return;
  for (const el of tr.querySelectorAll("[data-family]")) {
    if (el.disabled || el.readOnly) continue;
    el.value = "";
  }
  // 空にした行は、写した値も含めて無くなった
  forgetCarried(row);
  await settle(row, "");
  toast(`${row}行目を空にしました`, "ok");
}

/** 「重量を変更しますか？」。**はい**なら、その行だけ計算し直す。 */
async function askWeight(ask) {
  if (!confirm(ask.message)) return;      // いいえ ── 入っている値のまま
  try {
    paint(await api.post("/api/entry/state",
                         { ...collect(), row: ask.row,
                           redo_weight: [ask.row] }));
    toast(`${ask.row}行目の重量を ${ask.next} にしました`, "ok");
  } catch (err) {
    toastError(err);
  }
}

/*
  自動保存 (`NippouDB_AutoSave` 相当)。

  **書いてよいかはサーバが決める。** ここは「入力が動いた」と伝えるだけ。
  間引きの間隔も、呼出モード中に他の直へ書かないことも、画面には判断
  できない ── タブを開き直せば間引きは最初からになるし、いま何を開いて
  いるかもサーバしか知らない。

  応答の `saved` が false なら、まだその時ではなかっただけ(理由は
  `skipped`)。**失敗ではないので騒がない。**
*/
async function maybeAutosave() {
  // **打てるタブでなければ送らない。** サーバは 409 で断るので害は
  // ありませんが、断られるだけの要求を毎回飛ばすとログが埋まり、
  // 本当に断られた回が読めなくなります
  if (!tabLock.mayEdit()) return;
  const note = document.getElementById("save-note");
  try {
    // `mute`: 断られても「断られた」の音にしない(押していない場面で驚かせない ──
    // 下のトーストと同じ理由)
    let since = 0;
    const body = await inOrder(() => {
      since = editSeq;
      return api.post("/api/entry/save", { ...collect(), silent: true }, { mute: true });
    });
    if (body.saved) savedSeq = Math.max(savedSeq, since);
    // **見送られた理由が「直が変わった」なら、帯にも出す。** 1分ごとの
    // 見張りが拾うより先に分かるので、待たせない
    const moved = body.shift_changed;
    if (moved) showShiftMoved(moved.crossed ? moved.message : "");
    if (!note) return;
    if (body.saved) {
      note.textContent = `自動保存: ${body.saved_at || ""}`;
      note.dataset.state = "saved";
    } else {
      note.textContent = body.skipped || "";
      note.dataset.state = "waiting";
    }
  } catch (err) {
    // 自動保存の失敗はトーストにしない(操作していない場面で驚かせる)。
    // 明示的な保存のときに同じ理由が出る
    if (note) {
      note.textContent = "自動保存できませんでした";
      note.dataset.state = "failed";
    }
  }
}

/*
  集計CSVの書き出し結果。**出せなかったときだけ、はっきり出します。**

  保存は成功しているので騒ぎ立てません ── ただ、共有のフォルダへ届いて
  いないことに気づかないと、**古いCSVを最新だと思って読む人が出ます。**
  出せたときは出し先を静かに添えるだけ(`out-dir` の行に出ています)。
*/
function showExport(result) {
  const note = document.getElementById("export-note");
  if (!note) return;
  if (!result) {                    // 自動保存のときは付いてこない
    note.hidden = true;
    return;
  }
  note.hidden = false;
  note.textContent = result.message || "";
  note.className = result.ok ? "msg msg--info" : "msg msg--warn";
}

/**
 * 断り1件ずつに「どの直の、どのページの話か」を添える。
 *
 * サーバはページと行(`page` / `row`)を持たせて返しますが、**どの直の
 * ものか**は結果ぜんぶで1つ(チェック・保存)か、直ごとの束(共有へ保存)
 * です。開くボタンを出すにはその2つを1件ずつに落とす必要があるので、
 * ここで混ぜます。
 */
function withAt(findings, key) {
  return (findings || []).map((f) => ({
    ...f,
    at: {
      report_date: key?.report_date || "", line: key?.line || "",
      shift: key?.shift || "", page: f.page || 0,
    },
  }));
}

/**
 * 断りに書いてある場所へ、実際に行く。
 *
 * **「そのページを開いて直してください」だけでは開けません。**
 * 9月15日の1直を名指しで断られても、そこへ行く道が画面に無く、
 * 「1直は見たくても見れない　当然治せない」で止まっていました。
 *
 * 行き方は2つあり、**どちらになるかは相手が誰の記録か**で決まります:
 *
 *     いまの直・いまのライン … ページ移動(`/api/settings/page`。誰でも)
 *     それ以外               … 呼び出し(`/api/settings/recall`。管理者)
 *
 * 通してよいかを決めるのはサーバ(`recall_refusal`)なので、ここは
 * どちらの口を叩くかだけを選びます。
 */
async function openFinding(at) {
  const el = document.getElementById("sheet-key");
  const sameShift = el
    && el.dataset.openedDate === at.report_date
    && el.dataset.openedShift === at.shift
    && (el.dataset.openedLine || "") === (at.line || "");
  try {
    // 移る前に打ちかけを置く。**確定はしない**(`draft`)── 直しに行く
    // ために開くのだから、直っていないことを理由に止めては意味がない
    if (!await saveDraft()) return;
    const body = sameShift
      ? await api.post("/api/settings/page", { page: at.page || 1 })
      : await api.post("/api/settings/recall", {
          report_date: at.report_date, line: at.line,
          shift: at.shift, page: at.page || 1,
        });
    toast(body.message, "ok");
    location.href = body.next || "/";
  } catch (err) { toastError(err); }
}

/**
 * 確かめた結果を1か所に出す。**下見も、断られたときも同じ場所。**
 *
 * 出どころは3つあります(「この直をチェック」/「保存(確定)」に断られた /
 * 「共有へ保存」に断られた)が、**見た目は1つ**にします ── 同じ7項目
 * なので、場所や形が変わると別のものに見えます。
 */
function showFindings(message, ok, findings) {
  const panel = document.getElementById("check-panel");
  const head = document.getElementById("check-head");
  const list = document.getElementById("check-list");
  if (!panel || !head || !list) return;
  head.textContent = message;
  head.className = `msg msg--${ok ? "ok" : "error"}`;
  list.replaceChildren(...(findings || []).map(findingItem));
  panel.hidden = false;
}

/**
 * 断り1件を `<li>` にする。**出す場所が変わっても同じ形。**
 *
 * 「この直をチェック」も、断られたときも、押す前から出している帯
 * (`#shift-findings`)も、ここを通ります ── 同じ7項目なので、場所で
 * 形が変わると別のものに見えます。
 */
function findingItem(f) {
  const li = document.createElement("li");
  li.textContent = (f.where ? `${f.where} ` : "") + f.message;
  // **「いま直せ」と「直の終わりに要る」は別の話。**
  // 8時に1行目を打った人に「17時まで入れろ」とだけ出ると、
  // 打ち間違いだと思って探し始めます
  if (f.at_shift_end) {
    const when = document.createElement("b");
    when.className = "badge badge--todo";
    when.textContent = "直の終わりまでに";
    li.prepend(when, " ");
  }
  if (f.how) {
    const how = document.createElement("span");
    how.className = "lead";
    how.textContent = ` — ${f.how}`;
    li.append(how);
  }
  // **直しに行くボタンを、断りの隣に置く。** 「1ページ 1行目」と
  // 言われても、いま出ているのが2ページ目なら、その行は画面のどこにも
  // ありません ── 「停止内訳にない記号です　いや何も入ってませんけど」
  // は、これで起きていました
  const el = document.getElementById("sheet-key");
  const sameShift = f.at && el
    && el.dataset.openedDate === f.at.report_date
    && el.dataset.openedShift === f.at.shift
    && (el.dataset.openedLine || "") === (f.at.line || "");
  // **いま出ているものへ行くボタンは出しません。** ページの分からない
  // 断り(直ぜんぶの話。休憩・作業時間の合計)は、その直を開いていれば
  // もう目の前にあります
  const here = sameShift
    && (!f.at.page
        || Number(el.dataset.openedPage || 0) === Number(f.at.page));
  if (f.at && f.at.report_date && f.at.shift && !here) {
    const go = document.createElement("button");
    go.type = "button";
    go.className = "btn btn--sm";
    go.style.marginInlineStart = "var(--sp-2)";
    go.textContent = f.at.page
      ? `${f.at.report_date} ${f.at.shift} ${f.at.page}ページ目を開く`
      : `${f.at.report_date} ${f.at.shift} を開く`;
    go.addEventListener("click", () => { openFinding(f.at); });
    li.append(" ", go);
  }
  return li;
}

/**
 * この直の直すところ ── **押す前から出しておく帯。**
 *
 *     設定タブ内で不備は当然確認できるとしても（管理者用）
 *     入力しか一般作業者は開かないので
 *     そこで何故ダメかが見えないと意味がないかと
 *
 * 7項目の結果が画面に出るのは、これまで**何かを押して断られたとき**
 * だけでした。前の直のぶんは上の帯が理由まで残していたのに、自分が
 * いま打っている直は、直の終わりに断られるまで分かりません。
 *
 * **分けるのはサーバ**(`logic/entry_findings`)。ここは写すだけです ──
 * 「いま直すもの」と「直の終わりまでに」を混ぜると、1行目から赤い
 * 画面になって、そのうち誰も読まなくなります。
 */
function paintShiftFindings(standing) {
  const box = document.getElementById("shift-findings");
  const head = document.getElementById("shift-findings-head");
  const list = document.getElementById("shift-findings-list");
  if (!box || !head || !list) return;
  if (!standing || !standing.counted) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.dataset.level = standing.level || "";
  // **主語を先に。** 上の赤い帯は前の直、この帯はいまの直のこと
  const about = document.getElementById("shift-findings-about");
  if (about) about.textContent = standing.about || "";
  head.textContent = standing.headline || "";
  // **いま直すものが先。** 終わりまでのぶんは後ろに、灰色の印つきで
  const rows = [...(standing.now || []), ...(standing.at_end || [])];
  const at = {
    report_date: standing.report_date, line: standing.line,
    shift: standing.shift,
  };
  list.replaceChildren(...rows.map(
    (f) => findingItem({ ...f, at: { ...at, page: f.page || 0 } })));
}

/*
  保存(確定)。

  **直の変わり目をまたいでいたら、サーバが 409 で止めます**(書く前に)。
  そのときは「どちらの直へ入れるか」を出し、選ばれた答えを添えて
  投げ直します ── 選択肢も文言もサーバのもので、ここは出して返すだけ。

  `shiftChoice` が付いているのは、その投げ直しのときだけです。

  **書けたかどうかを返します。** ページ移動は「先に保存してから移る」ので、
  書けていないのに移ると打ちかけが消えます。
*/
async function saveNow(shiftChoice = "") {
  try {
    // 確定保存には `silent` を付けない。**間引きの対象外**で、
    // 押したときは必ず書く(サーバが判断するのは自動保存だけ)
    let since = 0;
    const body = await inOrder(() => {
      since = editSeq;
      const payload = collect();
      if (shiftChoice) payload.shift_choice = shiftChoice;
      return api.post("/api/entry/save", payload);
    });
    if (body.saved) savedSeq = Math.max(savedSeq, since);
    paint(body);
    // 書けたのだから、帯の知らせはもう役目を終えている
    if (body.saved) showShiftMoved("");
    showExport(body.export);
    return Boolean(body.saved);
  } catch (err) {
    if (err.status === 409 && err.body?.shift_changed?.crossed) {
      // **断りではなく、聞き返し。** トーストは出さない ── 数秒で
      // 消えるものに「どちらへ入れるか」を任せられない
      if (err.body) paint({ ...err.body, message: "" });
      showShiftMoved(err.body.shift_changed.message);
      askShift(err.body.shift_changed);
      return false;
    }
    // 422(LOT重複)は画面ぜんぶも返ってくる。断られた画面が
    // 古いままにならないよう、そちらも写す
    if (err.body) paint({ ...err.body, message: "" });
    // 保存前チェックで断られたときは、**「この直をチェック」と同じ場所へ**。
    // トーストは数秒で消えるので、7項目の断りをそこだけに載せると、
    // 目を離した隙に「なぜ保存できないのか」が画面から消えます
    if (err.body?.check_findings?.length) {
      showFindings(err.body.message, false,
                    withAt(err.body.check_findings, err.body));
      if (err.body.sound) {
        playKey(err.body.sound).catch(() => { /* 文言は出ている */ });
      }
    }
    toastError(err);
    return false;
  }
}

/**
 * 打ちかけのまま置く。**確定ではないので、7項目の関門を通りません。**
 *
 * ページを見に行く前に呼びます ── 見るだけの操作を関門で止めると、
 * 「1ページ目が直っていないので1ページ目を開けない」になります。
 * 直すために開くのですから、そこは通します。
 *
 * 置けたかどうかを返します(直の変わり目の聞き返しなど、置けない場面は
 * まだあります ── そのときは移りません)。
 */
async function saveDraft() {
  try {
    let since = 0;
    const body = await inOrder(() => {
      since = editSeq;
      return api.post("/api/entry/save", { ...collect(), draft: true });
    });
    if (body.saved || body.skipped) savedSeq = Math.max(savedSeq, since);
    paint({ ...body, message: "" });
    // **打っていない紙は「書けなかった」ではありません。**
    // サーバは1行も打っていない紙を作りません(空の紙を残さないため)。
    // ここを「保存できなかった」と読むと、移る手前で止まります
    return Boolean(body.saved) || Boolean(body.skipped);
  } catch (err) {
    if (err.body) paint({ ...err.body, message: "" });
    // **空の紙は、失いようがない。**
    //
    //     直してほしいといいつつないから直せないんですが
    //
    // 断りの「◯ページ目を開く」は、移る前に打ちかけを置きます。
    // その保存が「まだ1行も打っていません」で断られると、開くほうまで
    // 黙って止まっていました ── 開いたばかりの画面は空なので、
    // **直しに行こうとするときほど**止まります
    if (err.code === "empty_sheet") { savedSeq = Math.max(savedSeq, editSeq); return true; }
    toastError(err);
    return false;
  }
}

/** どちらの直へ入れるかを訊く。**選ばれるまで1行も書かれていない。** */
function askShift(moved) {
  const why = document.getElementById("shift-modal-why");
  const box = document.getElementById("shift-modal-choices");
  if (!why || !box) {
    // 置き場所が無い画面。**黙って諦めない** ── 選べないままだと
    // 保存できないので、せめて理由は出す
    toast(moved.message, "warn");
    return;
  }
  why.textContent = moved.message;
  box.replaceChildren();
  for (const choice of moved.choices || []) {
    const row = document.createElement("button");
    row.className = "pick__row";
    row.type = "button";
    row.innerHTML = "";
    const head = document.createElement("b");
    head.textContent = choice.label;
    const note = document.createElement("small");
    note.textContent = choice.note || "";
    row.append(head, note);
    row.addEventListener("click", () => {
      closeModal("shift-modal");
      saveNow(choice.value);
    });
    box.appendChild(row);
  }
  openModal("shift-modal");
}

/*
  [最新のページに戻る] (呼出モード中だけ出る)

  **直していた値を添えて送る。** サーバが呼出モードを解くより先に
  保存するので、「直して、戻る」で消えることがない
  (VBA `NippouDB_BackToCurrent` が先頭で silentMode 保存するのと同じ)。
*/
async function backToCurrent() {
  try {
    const body = await api.post("/api/settings/back", collect());
    toast(body.message, "ok");
    location.href = body.next || "/";
  } catch (err) { toastError(err); }
}

/**
 * 画面を出る前(ほかの画面へ・「終了」)。**打ちかけを置いてから出る。**
 *
 * 自動保存は1分に1回なので、そのあいだに打った行は画面にしか無い。
 * 置けなければ、移ってよいかを訊く(黙って捨てない)。
 */
async function keepTyped() {
  if (!unsaved() || !tabLock.mayEdit()) return true;
  if (await saveDraft()) return true;
  return confirm("打ちかけの行を保存できませんでした。\n"
                 + "このまま続けると、保存していない行は消えます。続けますか?");
}

export function start() {
  beforeLeave(keepTyped);
  // **どの欄でも、打ったら数える。** 打てる字に決まりのある欄(LOT・時刻)だけ
  // 数えていて、材・寸法のような自由な欄は「打っていない」扱いだった
  // (閉じる前に置かず、途中まで打った「A110」が残った)
  for (const name of ["input", "change"]) {
    document.addEventListener(name, (ev) => {
      const el = ev.target;
      if (el && el.matches
          && el.matches("[data-row][data-family], [data-header], [data-check]")) touched(el);
    }, { capture: true, signal: pageSignal() });
  }
  // 閉じる・読み直す(F5)。返事は待てないので、置くだけ送る
  window.addEventListener("pagehide", () => {
    if (unsaved() && tabLock.mayEdit()) {
      api.beacon("/api/entry/save", { ...collect(), draft: true });
    }
  }, { signal: pageSignal() });
  // **打つ画面はここだけなので、取り合うのもここだけ。** グラフや集計を
  // 2枚目で開くのはふつうの使い方なので、そこまで止めると邪魔になる
  tabLock.start();
  // **サーバが描いた「見るだけ」を、そのまま効かせる。** `paint()` を
  // 待つと最初の1回だけ打ててしまう(`applyReadOnly` に理由を書いてあります)
  applyReadOnly(!document.getElementById("read-only")?.hidden);
  // 直の始まりも同じ ── 帯はサーバが描いていますが、表を伏せるのは
  // JS の仕事なので、`paint()` を待つと**開いた直後だけ打ててしまいます**
  //
  // 前の直の始末も同じです。**サーバが描いた状態をそのまま効かせます**
  // ── ここを `paint()` に任せると、止まっているはずの画面が最初の
  // 1回だけ打ててしまい、関門の意味がなくなります
  const handoverBox = document.getElementById("handover");
  paintNeedsWorker({
    needs_worker: !document.getElementById("start-shift")?.hidden,
    needs_line: !document.getElementById("terminal-line-note")?.hidden,
    handover: { blocked: handoverBox?.dataset.blocked === "1" },
  });
  document.body.classList.toggle(
    "handover-blocked", handoverBox?.dataset.blocked === "1");

  // 前の画面のぶんが残っていることがある(ES モジュールは読み直されない)
  clearTimeout(settleTimer);
  askingRow = 0;
  lastProblem = "";
  // いま描かれている状態を出発点にする。`null` のままだと、最初の応答で
  // 「開いた瞬間」と誤って判定してポップアップが勝手に出る
  reasonAsked = "";

  markRow = 0;
  pickedHiki = "";
  endChipRow = 0;
  // 画面が持って来た「行ごとの印」を出発点にする。無いと、開いた直後に
  // 行を選んでもボタンが点かない(サーバへ1度投げるまで分からない)
  lastMarks = null;
  try {
    const seed = document.getElementById("entry-marks");
    if (seed) lastMarks = JSON.parse(seed.textContent);
  } catch (err) {
    console.warn("[entry] 最初の印を読めません:", err);
  }
  paintMarks(null);

  // **開いた最初の一瞬から、直すところを出す。**
  //
  //     「入力しか一般作業者は開かないので
  //       そこで何故ダメかが見えないと意味がないかと」
  //
  // 応答を1つ待ってから描くと、そのあいだ帯が空のままです ── 空の帯は
  // 「何も無い」と読まれるので、サーバが描いたぶんをここで1度写します。
  try {
    const seed = document.getElementById("shift-findings-data");
    if (seed) paintShiftFindings(JSON.parse(seed.textContent));
  } catch (err) {
    console.warn("[entry] この直の直すところを読めません:", err);
  }

  // ---- グリッド ----
  for (const el of inputs()) {
    el.addEventListener("focus", () => {
      const tr = el.closest("tr");
      for (const other of document.querySelectorAll("#grid-body tr")) {
        delete other.dataset.focus;
      }
      if (tr) tr.dataset.focus = "1";
      // etcの押しボタンは**いま選んでいる行**に効く(VBA の RowCount)
      markRow = Number(el.dataset.row) || 0;
      paintMarks(lastMarks);
    });
    el.addEventListener("blur", () => {
      clearTimeout(settleTimer);
      // ロット番号は、**打ち終わった時点**で `maybeLookup` がもう引いて
      // います。離れたときは、まだ引いていないぶんだけ引く(貼り付けの
      // 直後に離れた場合など)。同じ番号を二度引かないのは向こうの仕事
      if (el.dataset.family === "LOT") {
        // 打ち終わった時点で `maybeLookup` がもう引いています。まだ
        // 引いていないぶん(貼り付けた直後に離れた等)だけここで引く
        if (!maybeLookup(el)) {
          settleTimer = setTimeout(() => settle(Number(el.dataset.row)),
                                   SETTLE_MS);
        }
        return;
      }
      // **どの欄を直したかを添える。** 重量を計算し直してよいか聞くのは
      // 個装単位 枚数・梱包単位 包数 を直したときだけなので(サーバの
      // `calculations.weight_needs_asking`)、欄の名前が要る
      settleTimer = setTimeout(
        () => settle(Number(el.dataset.row), el.dataset.family), SETTLE_MS);
    });
    // 選ぶ欄(合紙・停止理由の記号)は、選んだ時点で決まる ── 離れるのを
    // 待つと、選んだのに合計が動かない時間ができる
    if (el.tagName === "SELECT") {
      el.addEventListener("change",
                          () => settle(Number(el.dataset.row),
                                       el.dataset.family));
      guardSelect(el);
    } else {
      wireLimits(el);
    }
  }

  // ---- ヘッダー ----
  for (const el of document.querySelectorAll("[data-header]")) {
    el.addEventListener("blur", () => settle());
    // **他から入れられたときも送る。** 「作業者を選ぶ」は値を書き込む
    // だけなので、blur が起きない ── 送らないと、次に何か打つまで
    // サーバは担当者が空のままだと思っている(手順の帯も進まない)
    el.addEventListener("change", () => settle());
  }
  document.getElementById("hd-day-shift")?.addEventListener("change", () => settle());

  // ---- etc の押しボタン ----
  for (const btn of document.querySelectorAll("[data-mark]")) {
    btn.addEventListener("click", async () => {
      // **1度目から効かせる。** 行が決まっていなければここで決める
      // (`paintMarks` に、押せなくしていた頃の話を書いてあります)
      markRow = rowForMarks();
      paintMarks(lastMarks);
      try {
        paint(await api.post("/api/entry/mark",
                             { ...collect(), row: markRow, key: btn.dataset.mark }));
      } catch (err) { toastError(err); }
    });
  }

  // ---- 引当を選ぶ ----
  document.getElementById("hiki-apply")?.addEventListener("click", async () => {
    if (!pickedHiki) { toast("引当を選んでください", "warn"); return; }
    const row = Number(document.getElementById("hiki-apply")?.dataset.row || 0);
    closeModal("hiki-modal");
    if (row) await lookupLot(row, pickedHiki);
  });

  // ---- 理由 (「その他」を選んだ行ごと) ----
  //
  // 欄そのものは `paintReason` が行ごとに作ります。ここはポップアップで
  // 書いたものを、その行の欄へ戻すところだけ
  document.getElementById("reason-apply")?.addEventListener("click", () => {
    const text = document.getElementById("reason-text");
    const row = text?.dataset.row;
    const field = row && document.getElementById(`reason-${row}`);
    if (field && text) {
      field.value = text.value.trim();
      settle();
    }
    closeModal("reason-modal");
  });

  // 最初に描かれている欄にも配線する(以後は `paintReason` が付ける)
  for (const el of document.querySelectorAll("[data-reason]")) {
    el.addEventListener("blur", () => settle());
  }

  // ---- チェックボックスの排他制御 ----
  for (const el of document.querySelectorAll("[data-check]")) {
    el.addEventListener("change", async () => {
      try {
        paint(await api.post("/api/entry/check",
                             { ...collect(), name: el.dataset.check }));
      } catch (err) { toastError(err); }
    });
  }

  // ---- ライン ----
  //
  // **この画面からは変えません。** どのラインの端末かは据え付けのときに
  // 決まるもので、毎直選び直すものではありません(`app/routes/entry.
  // change_line` の説明)。変えるのは設定・管理者の「この端末のライン」。

  // ---- 保存 ----
  document.getElementById("save")?.addEventListener("click", () => saveNow());

  // ---- 新しいページ (VBA の「新規発行」) ----
  //
  // **いまのページを保存してから移る。** 押した人が2度手間を踏まないよう、
  // 保存もページ作りもサーバが1度で済ませる(途中で落ちても、打った12行は
  // 先に書かれている)。
  /*
    ---- 行を空にする (✕) ----

    **表そのものに委ねる。** 12個のボタンを1つずつ繋ぐと、ページを
    描き直すたびに繋ぎ直すことになります(`nav` の画面差し替えで実際に
    起きました)。表に1つだけ聞き手を置いて、押された行を読みます。
    確かめは ✕ のすぐ下(`askClearRow`)。
  */
  document.getElementById("grid-body")?.addEventListener("click", (event) => {
    const btn = event.target.closest?.("[data-clear-row]");
    if (!btn) return;
    if (askingRow === Number(btn.dataset.clearRow)) { closeRowAsk(); return; }
    askClearRow(btn);
  });
  {
    const signal = pageSignal();
    document.getElementById("row-ask-yes")?.addEventListener("click", () => {
      const row = askingRow;
      closeRowAsk();
      if (row) clearRow(row).catch(toastError);
    });
    document.getElementById("row-ask-no")?.addEventListener(
      "click", () => closeRowAsk({ refocus: true }));
    // ほかの所を押した・Esc・表や画面が動いた ── 何もせずに閉じる
    document.addEventListener("pointerdown", (event) => {
      if (!event.target.closest?.("#row-ask, [data-clear-row]")) closeRowAsk();
    }, { signal });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && askingRow) closeRowAsk({ refocus: true });
    }, { signal });
    document.addEventListener("scroll", () => closeRowAsk(), { signal, capture: true });
    window.addEventListener("resize", () => closeRowAsk(), { signal });
  }
  // 開始・終了の時/分のダブルクリック。**欄ごとに繋がず、表に1つ**(上と同じ理由)
  // 枚数・包数のダブルクリックは「上の行と同じ」(v4.7.0)
  document.getElementById("grid-body")?.addEventListener("dblclick", (event) => {
    const input = event.target.closest?.("input[data-stamp]");
    if (input) {
      event.preventDefault();
      stampTime(input);
      return;
    }
    const pack = event.target.closest?.("input[data-copy-above]");
    if (pack) {
      event.preventDefault();
      copyAbove(pack);
    }
  });

  // Enter で次の欄・Shift+Enter で前の欄(v4.7.0)。**表に1つ**
  document.getElementById("grid-body")?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    // 日本語の変換を確定する Enter は取らない
    if (event.isComposing || event.keyCode === 229) return;
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const el = event.target.closest?.("[data-enter-next]");
    if (!el) return;
    event.preventDefault();
    moveByEnter(el, event.shiftKey);
  });

  // 「直の終わり」── 終了の時/分に入ったら、その下に出す
  {
    const signal = pageSignal();
    const chipBtn = document.getElementById("shift-end-btn");
    // **押しても欄から焦点を外さない。** 外れると押す前に消えてしまう
    chipBtn?.addEventListener("pointerdown", (event) => event.preventDefault());
    chipBtn?.addEventListener("click", () => {
      const row = endChipRow;
      hideEndChip();
      stampShiftEnd(row);
    });
    document.getElementById("grid-body")?.addEventListener("focusin", (event) => {
      const el = event.target;
      if (el?.dataset?.family === "SZ" || el?.dataset?.family === "SH") showEndChip(el);
      else hideEndChip();
    });
    document.getElementById("grid-body")?.addEventListener("focusout", () => {
      // 隣の欄(時 → 分)へ移るときに一瞬消えないよう、少し待ってから見る
      clearTimeout(endChipTimer);
      endChipTimer = setTimeout(() => {
        const family = document.activeElement?.dataset?.family;
        if (family !== "SZ" && family !== "SH") hideEndChip();
      }, 120);
    });
    // 表や画面が動いたら**付いて行く**(欄へ移ったときの自動の送りでも動くので、
    // 消すと出た途端に消えてしまう)
    const follow = () => {
      if (!endChipRow) return;
      const el = document.activeElement;
      if (el?.dataset?.family === "SZ" || el?.dataset?.family === "SH") showEndChip(el);
      else hideEndChip();
    };
    document.addEventListener("scroll", follow, { signal, capture: true });
    window.addEventListener("resize", follow, { signal });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") hideEndChip();
    }, { signal });
  }

  document.getElementById("new-page")?.addEventListener("click", async () => {
    /*
      **押す前に、いまの12行で確かめ直す**(v4.8.0)。

      前は先に「よろしいですか?」と聞いてから送り、OK を押したあとで
      「12行目まで埋めてから」と断られていました ── 断るなら聞く前に。
      手元の案内は最後に塗ったときのもので、打った直後に押すと古いことが
      あるので、いまの値を1度送って決め直してもらいます(決めるのはサーバ)。
    */
    let guide = null;
    try {
      const fresh = await api.post("/api/entry/state", collect());
      paint(fresh);
      guide = fresh.page_guide;
    } catch (err) {
      toastError(err);
      return;
    }
    if (guide && !guide.ready) {
      toast(guide.reason || "まだ新しいページは出せません", "warn");
      return;
    }
    // 聞くことは1回にまとめてある(空いた行・いつもより多いページ・何が起きるか)
    if (!confirm(guide?.confirm
                 || "いまのページを保存して、次のページを発行します。よろしいですか?")) return;
    /*
      いつもよりページが多いとき、サーバが 422 + `needs_confirm` で聞き返す
      (`logic/pages.new_page_warning`)。**文言はサーバのものをそのまま
      出す** ── 何ページ目なのか・いつもの上限が何ページなのかを知っているのは
      サーバの側で、ここで書くと2か所で食い違う。

      はいと言われたら `confirm` を添えて同じ道へ投げ直す。**止めるのでは
      なく、確かめるだけ**なので、断り方は用意しない。
    */
    const send = (extra = {}) =>
      api.post("/api/entry/newpage", { ...collect(), ...extra });
    try {
      let body;
      try {
        // 上で聞いたぶんは「はい」をもらってある
        body = await send({ confirm: true });
      } catch (err) {
        if (!err.body?.needs_confirm) throw err;
        if (!confirm(err.message)) return;
        body = await send({ confirm: true });
      }
      toast(body.message, "ok");
      // ページが変わったので画面ごと引き直す(空の12行が返ってくる)
      refresh();
    } catch (err) {
      if (err.body) paint({ ...err.body, message: "" });
      // 発行も**確定の入口**なので、断りは「この直をチェック」と
      // 同じ場所へ。トーストだけだと、消えたあとに理由が残りません
      if (err.body?.check_findings?.length) {
        showFindings(err.body.message, false,
                    withAt(err.body.check_findings, err.body));
        if (err.body.sound) {
          playKey(err.body.sound).catch(() => { /* 文言は出ている */ });
        }
      }
      toastError(err);
    }
  });

  document.getElementById("back-to-current")
    ?.addEventListener("click", backToCurrent);
  // 案内の箱の「最新のページに戻る」(前のページを直しているとき)
  document.getElementById("page-guide-back")
    ?.addEventListener("click", backToCurrent);

  /*
    ---- ページを切り替える (VBA `NippouDB_EditPage`) ----

    **先に、いま打っているページを打ちかけのまま置く。** 移ってから
    「さっきの行が無い」は取り返しがつかない ── 押した人は「ページを
    見に行く」としか思っていないので、置かずに移ると黙って消えます。

    **置くだけで、確定はしません**(`draft: true`)。確定にすると、
    1ページ目に間違いがあるだけで**1ページ目を見に行けなくなります** ──
    直すために開きたいのに、直っていないから開けない。止めるのは
    「保存(確定)」と「共有へ保存」で、見に行くことは止めません。

    動くのはページだけで、日付も直もラインも動きません。管理者モードは
    要りません(`services/nippou_service.edit_page` の説明)。
  */
  document.getElementById("page-switch")?.addEventListener("change", async (ev) => {
    const wanted = Number(ev.target.value);
    const now = Number(ev.target.dataset.now || 0);
    if (!wanted || wanted === now) return;
    try {
      // **置けていなければ移らない。** 直の変わり目で聞き返されている
      // 最中に移ると、選ぶ前に打ちかけが画面から消えます
      if (!await saveDraft()) {
        ev.target.value = String(now || wanted);
        return;
      }
      const body = await api.post("/api/settings/page", { page: wanted });
      toast(body.message, "ok");
      refresh();
    } catch (err) {
      // 戻せないと、選んだページと出ている中身が食い違ったままになる
      ev.target.value = String(now || wanted);
      toastError(err);
    }
  });

  /*
    ---- 紙の束の札を押した ----

    **押す先は2つで、行き先が違います**(どちらかはサーバが決めていて、
    札に `data-sheet-open` で入っています ── `logic/sheet_strip`):

        page  … 同じ直の別ページ。**その場で切り替える**
                自分がさっき打った紙を自分で見直すだけなので、
                管理者モードは要りません(ページ選択欄と同じ道)
        paper … 別の直の紙。**読むだけで開く**(別窓)
                直すのは「記録を見る」から呼び出す道(管理者モード)で、
                ここから書き換えられるようにはしません

    いま開いている札は空なので、押しても何も起きません。

    札は応答のたびに組み直されるので、**受けるのは並びの側**です
    (札ごとに付けると、組み直した瞬間に効かなくなります)。
  */
  document.getElementById("sheets-row")?.addEventListener("click", async (ev) => {
    const tab = ev.target?.closest?.("[data-sheet-open]");
    if (!tab) return;
    const how = tab.dataset.sheetOpen;
    if (!how) return;                    // いま開いている紙
    const key = document.getElementById("sheets")?.dataset || {};
    if (how === "paper") {
      // **その直ぜんぶ**を出す ── 1ページだけ出しても、隣のページに
      // 書いてあることが読めません(「印刷」と同じ出しかた)
      const q = new URLSearchParams({
        report_date: key.sheetsDate || "", line: key.sheetsLine || "",
        shift: tab.dataset.sheetShift || "", page: "all",
      }).toString();
      openPage(tokenUrl(`/report/nippou?${q}`), "日報の紙");
      return;
    }
    const wanted = Number(tab.dataset.sheetPage);
    if (!wanted) return;
    try {
      // **置けていなければ移らない。** 移った先で打ちかけが消えます
      if (!await saveDraft()) return;
      const body = await api.post("/api/settings/page", { page: wanted });
      toast(body.message, "ok");
      refresh();
    } catch (err) {
      toastError(err);
    }
  });

  /*
    ---- この直をチェック (VBA `ExecutePrintProcess` の5つの手続き) ----

    **押しても保存しませんし、止まりもしません。** やっているのは1つ ──
    保存済みのこの直の全ページを読み直して、「共有へ保存」が断る**7項目**に
    当ててみて、結果を並べるだけです。止めるのは「共有へ保存」の側で、ここは
    直の途中でも押せる下見です ── 直の終わりに断られてから12行を
    見直すより、途中で気づけるほうが直しやすい。

    画面に出ているページは**画面の中身**(打ったとおり)で見ます(v4.18.0)。
    以前は保存済みだけを見ていて、打った休憩が「保存(確定)」まで届かず
    「入れてるんだけどずっと出てますね」になっていました。まだ保存していない
    中身で見たときは、サーバが結果にそう添えます。
  */
  document.getElementById("check-shift")?.addEventListener("click", async () => {
    try {
      // 画面の中身も送る。**画面のページは打ったとおりで確かめる**(v4.18.0)
      const body = await api.post("/api/entry/verify", collect());
      showFindings(body.message, body.ok, withAt(body.findings, body));
      // 鳴らすのは1つだけ。VBA も1件目で鳴らして止めていた
      if (body.sound) playKey(body.sound).catch(() => { /* 文言は出ている */ });
    } catch (err) {
      toastError(err);
    }
  });

  /*
    ---- 共有へ保存 ----

    **押す場所を、押す人のいる画面に置く。** これは設定画面の奥にしか
    ありませんでした ── 直の終わりに毎回押すものが、設定を変えるための
    画面の中にあると、手順を覚えている人しか押せません。

    断られたとき(422)は、上の「確かめる」と**同じ場所に同じ形で**出します。
    別の見た目で出すと、同じ7項目だと分かりません。

    逃げ道(「設備移動のため保存」)はここには置きません ── 途中まででも
    通す判断は、書き先のパスや未保存の数を見ながらするものなので、
    設定画面の側に残します。断り文からそちらへ誘導します。
  */
  /**
   * 共有へ保存する。`skip` は「設備移動のため保存」の打ち込み。
   *
   * **断りは、この画面で完結させます。** 以前は「逃げ道は設定・管理者へ」
   * と誘導していましたが、**設定画面まで歩かせておいて、そこで同じ物を
   * もう1度読ませる**ことになります。打つ欄はこの下にあります。
   */
  async function pushShared(skip = "") {
    if (!(await confirmPush())) return;
    const note = document.getElementById("save-note");
    // **進み具合を出す**(確かめる → 送る → 写す → 月替わり)。`progress.js`
    const stop = watchJob("共有へ保存しています");
    try {
      // **押した人を添える。** 共有保存の履歴に「押した作業者」として残る
      // (いま欄に出ている作業者。前の直の残りを送るときは担当者と違う)
      const worker = document.querySelector('[data-header="worker"]')?.value || "";
      const body = await api.post("/api/settings/push",
                                  skip ? { skip, worker } : { worker });
      document.getElementById("check-panel").hidden = true;
      hideSkipBox();
      if (note) note.textContent = body.message || "";
      toast(body.message || "共有へ保存しました", body.failed ? "warn" : "ok");
      refresh();
    } catch (err) {
      if (err.status === 422 && err.body && err.body.reports) {
        // 直が複数まとまって断られることがある(共有へ未保存が溜まって
        // いるとき)。**どの直の話かを付けて**並べないと直しようがない
        const findings = [];
        for (const report of err.body.reports.filter((r) => !r.ok)) {
          const shift = `${report.report_date} ${lineLabel(report.line)} ${report.shift}`;
          for (const f of withAt(report.findings, report)) {
            findings.push({ ...f, where: `${shift} ${f.where || ""}`.trim() });
          }
        }
        showFindings(err.message.split("\n")[0], false, findings);
        // **欄はいつでも出します。**
        //
        // 前は「通せるものだけのとき」しか出しませんでした。ところが
        // 断り文のほうは必ず「下に『設備移動のため保存』と打つと通せます」
        // と書くので、**欄の無い画面で「下に打て」と読まされる**ことに
        // なります(「入力できるような状態で表示すべき内容です」)。
        //
        // 通せないものが混ざっているときは、欄は出したまま打てなくし、
        // **何が残っているか**を並べます ── 打っても通らないことを、
        // 打つ前に読めるように。通せるかどうかを決めるのはサーバです
        // (`save_checks.unskippable`)
        showSkipBox(findings.length > 0, err.body.skip_phrase || "",
                    err.body.skip_blockers || []);
        if (note) note.textContent = "";
        return;
      }
      toastError(err);
    } finally {
      stop();                 // **必ず消す**(枠が出たままになる)
    }
  }

  function hideSkipBox() {
    const box = document.getElementById("entry-skip");
    if (box) box.hidden = true;
    // **伏せるときに戻しておく。** 次に出したときが「通せるものだけ」
    // でも、前回止めたままだと打てません
    const input = document.getElementById("entry-skip-phrase");
    const push = document.getElementById("entry-skip-push");
    if (input) input.disabled = false;
    if (push) push.disabled = false;
    const warn = document.getElementById("entry-skip-blocked");
    if (warn) warn.hidden = true;
  }

  /**
   * 逃げ道の欄。`blockers` は**この逃げ道では通せない**断りたち。
   *
   * 1件でもあれば、欄は出したまま打てなくし、何が残っているかを並べます
   * ── 「打っても通らない」を打つ前に読めるように。
   */
  function showSkipBox(show, phrase, blockers = []) {
    const box = document.getElementById("entry-skip");
    if (!box) return;
    box.hidden = !show;
    if (!show) return;
    const text = document.getElementById("entry-skip-text");
    const input = document.getElementById("entry-skip-phrase");
    const push = document.getElementById("entry-skip-push");
    if (phrase && text) text.textContent = phrase;
    if (phrase && input) input.placeholder = phrase;
    for (const echo of box.querySelectorAll(".entry-skip-phrase-echo")) {
      if (phrase) echo.textContent = phrase;
    }

    const warn = document.getElementById("entry-skip-blocked");
    const list = document.getElementById("entry-skip-blockers");
    const blocked = blockers.length > 0;
    if (warn) warn.hidden = !blocked;
    // **「先に次を直してください」には、行く道を付ける。**
    //
    //     直してほしいといいつつないから直せないんですが
    //
    // 並べるだけでは、9月15日の1直を名指しされた人は記録を見るから
    // 探すしかありません(古い直は一覧からも溢れます)。`findingItem`
    // はサーバの `at` を見て「◯ページ目を開く」を添えます ── 上の
    // 断りの一覧と**同じ形**です
    if (list) {
      list.replaceChildren(...blockers.map(findingItem));
    }
    if (input) input.disabled = blocked;
    if (push) push.disabled = blocked;
    if (input && !blocked) input.focus();
  }

  document.getElementById("push-shared")?.addEventListener("click", () => {
    pushShared().catch(toastError);
  });

  /*
    ---- 前の直の始末 ----

    出口は2つ。**どちらも1押し**で、どちらも「直す」ことはしません。

      共有へ保存する      … 中身が綺麗なときはこれで済みます。
                            押すことは直すことではないので、誰でも押せます
      直せないまま引き継ぐ … 誰が・いつ・何を残したまま引き継いだかを
                            記録して、入力を始められるようにします

    引き継ぎは片付けではありません ── 共有へ出るまで帯は残ります。
  */
  document.getElementById("handover-push")?.addEventListener("click", () => {
    pushShared().catch(toastError);
  });

  document.getElementById("handover-take")?.addEventListener("click", async () => {
    const left = document.getElementById("handover-text")?.textContent || "";
    if (!confirm(`${left}\n\n直せないまま引き継ぎます。よろしいですか?`)) return;
    try {
      const body = await api.post("/api/entry/handover", {
        // 引き継いだ人の名前。**打ちかけの欄から送ります** ── まだ
        // 保存していない時点なので、サーバ側には入っていません
        worker: document.querySelector('[data-header="worker"]')?.value || "",
      });
      toast(body.message, "ok");
      refresh();
    } catch (err) { toastError(err); }
  });

  document.getElementById("entry-skip-push")?.addEventListener("click", () => {
    const input = document.getElementById("entry-skip-phrase");
    const typed = String(input?.value || "").trim();
    if (!typed) {
      toast("「設備移動のため保存」と打ってください", "warn");
      input?.focus();
      return;
    }
    // **合っているかを決めるのはサーバ**(`shift_check.normalize_skip`)。
    // ここで突き合わせると、言い回しを変えたときに2か所直すことになる
    pushShared(typed).catch(toastError);
  });

  /*
    「印刷」── **保存とは別のボタン。**

    VBA は1つのボタンで 保存 も 集計 も 印刷 もやっていましたが、紙は
    **要るときだけ**出すものにしました。押さなければ1枚も出ませんし、
    押しても保存にはなりません(直を確定したことにもなりません ──
    途中で1枚刷っただけで催促が止まると、そのあとに打った行が
    確かめられないまま直が終わります)。

    開くのは**この直ぜんぶ**(`page=all`)です。いま見ているページだけを
    出すと、3ページある直を刷るのに3回押すことになります。1つ開いて
    1回 Ctrl+P すれば、紙はページの数だけ出ます。

    出るのは**保存されているぶん**です。打ちかけを載せたいなら、先に
    「保存(確定)」を押す ── ここで勝手に保存すると、2つのボタンを
    分けた意味が無くなります。
  */
  document.getElementById("print-page")?.addEventListener("click", (event) => {
    const b = event.currentTarget.dataset;
    const q = new URLSearchParams({
      report_date: b.reportDate || "", line: b.line || "",
      shift: b.shift || "", page: "all",
    }).toString();
    openPage(tokenUrl(`/report/nippou?${q}`), "日報の紙");
  });

  // 前の直の警告を閉じる。**覚えるのはサーバ** ── 画面に覚えさせると
  // タブを開き直すたびに出て、読まずに閉じる癖がつく
  /*
    見るだけの帯から、いまの直へ戻る。

    **出口を、断られる場所に置く。** これまで「最新のページに戻る」は保存の並びの
    中にあり、打てない画面をスクロールした先でした(「過去データの終了方法
    ／管理者モードの終了方法 これらがわからない」)。断っている帯の中に
    置けば、探さずに済みます。

    サーバは戻る前に、開いていたぶんを保存します ── 見るだけの画面
    なので普通は変わっていませんが、管理者モードが途中で外れた場合は
    それまでに直したぶんがあります。
  */
  /*
    ---- 次の直を始める ----

    直の時間が過ぎて見るだけになった画面からの出口です。固定していた直を
    手放すと、書き先はまた時計から決まり、**作業者を選んだ時点で新しい直に
    固定されます**(`logic/shift_anchor`)。

    **閉じるからには出口を置きます。** 止まったまま次へ進む道が無いと、
    次に座った人は何を押せばよいのか分かりません。
  */
  document.getElementById("start-next-shift")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/entry/start-next", {});
      toast(body.message, "ok");
      // 開く紙が変わるので、値の入った古い画面を残せません
      location.href = body.next || "/";
    } catch (err) { toastError(err); }
  });

  document.getElementById("read-only-back")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/settings/back", {});
      toast(body.message || "いまの直に戻りました", "ok");
      // **画面ごと引き直す。** 開く紙が変わるので、値の入った古い画面を
      // 残せません
      location.href = "/";
    } catch (err) {
      toastError(err);
    }
  });

  document.getElementById("prev-shift-ack")?.addEventListener("click", async () => {
    document.getElementById("prev-shift").hidden = true;
    try {
      await api.post("/api/entry/prev-shift-ack", {});
    } catch (err) {
      // 閉じられればよい。次に開いたときまた出るだけ
    }
  });

  // 画面を出るときにタイマーを止める(`nav.js` が合図をくれる)
  pageSignal().addEventListener("abort", () => clearTimeout(settleTimer),
                                { once: true });
}
