/*
  views/calendar.js — カレンダー画面 (tkinter 版 CalendarWindow)

  **持つのは「ビューモデル → DOM」の写しと、選ばせる順番だけ。**
  どの日をどう塗るか・何を書くか・誰を選べるか・直を聞くかどうかは
  すべてサーバが決めている(docs/設計.md §1)。

  日付を押したあとの流れは tkinter 版と同じ:

    日付 → [休み / コメント・連絡 / 内容閲覧 / 削除]
             休み           → 作業者 → (3交替なら) 直 → 残業繋ぎ → 早出繋ぎ
             コメント・連絡 → ライン → 班 → 本文
             内容閲覧       → その日の登録内容(読むだけ)
             削除           → チェック → 最終確認
*/

import { openWindow } from "../desktop.js";
import { ApiError, api, tokenUrl } from "../api.js";
import { renderCells, renderHeaders } from "../grid.js";
import * as leave from "../leave.js";
import { NONE, confirm, inform, modal, pick } from "../modal.js";
import * as sync from "../sync.js";
import { toast, toastError } from "../toast.js";

const el = {};
let view = null;              // いま出している月のビューモデル
let loading = false;          // 二重読み込みを防ぐ
// 描いた時点の「取り込み直した時刻」と「今日」。**変わったら描き直す**
let drawn = { received: null, today: null };
let waiting = false;          // モーダルを閉じたら描き直す約束

/*
  【遅れて届いた返事で、いまの画面を上書きしない】
  月の読み込みは何本も重なりうる(翌月を押してすぐ今日を押す・登録の
  直後に背景の同期で読み直す)。以前は**届いた順に**描いていたので、
  遅れて届いた古い月の返事が、利用者が移った先の月を上書きしていた。
  また、登録の返事(登録した行が入っている)のあとに、登録より前に
  頼んだ読み直しの返事が届くと、**打った行が画面から消えた**ように見えた。

  頼むたびに番号を進め、**最後に頼んだものの返事だけ**を描く。
  登録も番号を進める(登録より前に頼んだ読み込みの返事は捨てる)。
*/
let loadSeq = 0;
// 最後に頼んだ月。「翌月」はここから数える(描いた月から数えると、
// 返事を待つあいだの2回目の「翌月」が同じ月を頼み直すだけになっていた)
let target = null;
// 登録・削除を送っている最中か。**そのあいだは背景の読み直しを待たせる**
let submitting = 0;

export function start() {
  el.grid = document.getElementById("cal-grid");
  el.headers = document.getElementById("cal-headers");
  el.title = document.getElementById("cal-title");
  el.notice = document.getElementById("cal-notice");

  document.getElementById("cal-prev").addEventListener("click", () => move(-1));
  document.getElementById("cal-next").addEventListener("click", () => move(1));
  document.getElementById("cal-today").addEventListener("click", () => {
    const now = new Date();
    load(now.getFullYear(), now.getMonth() + 1);
  });
  document.getElementById("cal-print").addEventListener("click", openPrint);

  // 「今すぐ同期」で取り込み直したら、内容が変わっているので描き直す
  document.addEventListener("app:synced", () => reload());

  // **開けっぱなしでも古い表示のままにしない。**
  // 背景の同期(既定20秒ごと)が他ラインの登録を手元へ入れても、押されない
  // かぎり描き直されなかった。帯だけが「同期済み」と出るので、
  // **古い画面を最新だと読ませてしまう**のが一番たちが悪い。
  // 見張りは `sync.js` の1本だけなので、そこに相乗りする。
  sync.subscribe(onSync);

  // キーボードだけでも月を移せる
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, .modal")) return;
    if (event.key === "PageUp") { event.preventDefault(); move(-1); }
    if (event.key === "PageDown") { event.preventDefault(); move(1); }
  });

  // **保存していない連絡を言えるようにしておく**(読み込み直し・窓の × の前に訊く)
  leave.register(() => {
    if (liveDraft && String(liveDraft.text || "").trim()) {
      return `${liveDraft.label} ${liveDraft.line} ${liveDraft.group}班 への連絡(まだ登録していません)`;
    }
    return liveRest;
  });

  load().then(restoreDraft);
}

/* ------------------------------------------------------------------
   読み込みと描画
   ------------------------------------------------------------------ */
async function load(year, month) {
  const query = (year && month) ? `?year=${year}&month=${month}` : "";
  const mine = ++loadSeq;
  if (year && month) target = { year, month };
  loading = true;
  try {
    const payload = await api.get(`/api/calendar${query}`);
    // **後から別の月を頼んでいたら描かない**(遅れて届いた古い返事)
    if (mine !== loadSeq) return;
    render(payload);
  } catch (err) {
    if (mine === loadSeq) toastError(err);
  } finally {
    if (mine === loadSeq) loading = false;
  }
}

/** いま出している月をそのまま読み直す。**月は勝手に動かさない。** */
function reload() {
  return load(view && view.year, view && view.month);
}

/**
 * 同期の状態が届くたびに、描き直すべきかを決める。
 *
 * 描き直すのは2つのとき:
 *   1. 取り込み直された (他ラインの登録が手元へ入った)
 *   2. 日付が変わった (「今日」の枠を動かす)
 *
 * **モーダルを開いているあいだは触らない。** 選んでいる途中で中身を
 * 入れ替えると、押した先が別のものになる。閉じたあと次の合図で描き直す。
 */
function onSync(status) {
  if (!status || loading) return;
  // 登録を送っている最中は読み直さない(登録より前の中身で上書きしない)。
  // 終わったあとの次の合図で読み直す
  if (submitting) { waiting = true; return; }

  const received = status.last_received_at || "";
  const today = status.today || "";
  if (drawn.received === null) {
    // 最初の1回は基準を覚えるだけ(描き直す理由が無い)
    drawn = { received, today };
    return;
  }
  const arrived = received && received !== drawn.received;
  const rolled = today && today !== drawn.today;
  if (!arrived && !rolled && !waiting) return;

  if (document.querySelector(".modal")) {
    waiting = true;             // 閉じるまで待つ
    return;
  }
  waiting = false;
  drawn = { received, today };
  reload();
}

function move(delta) {
  const base = target || view;
  if (!base) return;
  const index = base.year * 12 + (base.month - 1) + delta;
  load(Math.floor(index / 12), (index % 12) + 1);
}

function render(payload) {
  view = payload;
  // 描いた月を「最後に頼んだ月」にそろえる(範囲の外を頼んだときはサーバが
  // 端の月に丸めて返すので、次の「翌月」はそこから数える)
  target = { year: payload.year, month: payload.month };
  // 描いた時点の基準を先に更新する。**このあと `sync.apply` が
  // 購読者を呼ぶので、順番を逆にすると自分の更新で描き直しが走る**
  if (payload.sync) {
    drawn = { received: payload.sync.last_received_at || "",
              today: payload.sync.today || "" };
  }
  sync.apply(payload.sync);

  el.title.textContent = payload.title;
  document.getElementById("cal-prev").disabled = !payload.can_prev;
  document.getElementById("cal-next").disabled = !payload.can_next;

  if (payload.notice) {
    el.notice.textContent = payload.notice;
    el.notice.hidden = false;
  } else {
    el.notice.hidden = true;
  }

  const line = document.getElementById("rb-line");
  if (line) line.textContent = payload.my_line || "未設定";

  // マス目は履歴画面と同じ部品で描く(`grid.js`)
  renderHeaders(el.headers, payload.weekdays);
  renderCells(el.grid, payload.cells, onDayClicked);
  el.grid.setAttribute("aria-busy", "false");
}

/* ------------------------------------------------------------------
   日付を押したあと (tkinter 版 on_day_clicked)
   ------------------------------------------------------------------ */
async function onDayClicked(cell) {
  const label = `${Number(cell.date.slice(5, 7))}月${Number(cell.date.slice(8, 10))}日`;
  const action = await pick(`${label} の操作を選んでください`, [
    { label: "休み", value: "rest", note: "作業者の休みを登録" },
    { label: "コメント・連絡", value: "comment", note: "ライン・班あての連絡" },
    { label: "内容閲覧", value: "view", note: `登録 ${cell.count} 件` },
    { label: "削除", value: "delete", note: "履歴に残して削除" },
  ]);
  if (!action) return;

  try {
    if (action === "rest") await registerRest(cell.date, label);
    else if (action === "comment") await registerComment(cell.date, label);
    else if (action === "view") await viewDay(cell.date);
    else if (action === "delete") await deleteDay(cell.date, label);
  } catch (err) {
    toastError(err);
  }
}

/* -- 休み ---------------------------------------------------------- */
async function registerRest(date, label) {
  const options = await api.get("/api/workers");
  if (!options.total) {
    await inform("作業者が居ません",
                 "対象の作業者が見つかりません。\n"
                 + "設定画面で班員名簿の取り込みとライン設定を確認してください。");
    return;
  }

  const worker = await pickWorker(`${label} 休みにする作業者`, options,
                                  { step: "1 / 4", allowNone: false });
  if (!worker) return;

  const body = { date, code: worker.code };

  // **直を聞くかどうかはサーバが決める**(A/B/C/D 班だけ)。
  // 画面は `needs_shift` を見るだけで、班の一覧を持たない
  if (worker.needs_shift) {
    const shift = await pick(`${worker.name} さんの直`, [
      { label: "1直", value: "1" }, { label: "2直", value: "2" },
      { label: "3直", value: "3" },
    ], { step: "2 / 4" });
    if (!shift) return;
    body.shift = shift;

    // **繋ぎは作業者の一覧を続けて2回出す。** 同じ形の画面が並ぶので、
    // 見出しだけが変わると「選んだのが効いていない」ように見える。
    // どの直を繋ぐのか(`shift_links`)と、**ここまでに選んだもの**を
    // 一緒に出して、進んだことが分かるようにする
    const link = (options.shift_links || {})[shift] || {};
    const chosen = [
      { key: "休む人", value: `${worker.name}(${worker.group}班)` },
      { key: "直", value: `${shift}直` },
    ];

    // 繋ぎは「未登録のまま」を選べる。取り消し(null)とは別物なので、
    // NONE と区別する ── 空文字で送るとサーバが「未登録」で保存する
    const overtime = await pickWorker(
      link.overtime ? `${link.overtime}直の残業で繋ぐ人` : "残業繋ぎの担当者",
      options,
      { step: "3 / 4", allowNone: true, tone: "overtime",
        tag: "残業繋ぎ", chosen });
    if (overtime === null) return;               // 取り消し
    body.overtime = overtime === NONE ? "" : overtime.name;

    const early = await pickWorker(
      link.early ? `${link.early}直の早出で繋ぐ人` : "早出繋ぎの担当者",
      options,
      { step: "4 / 4", allowNone: true, tone: "early",
        tag: "早出繋ぎ",
        // **直前に選んだものを出す。** これが出ていれば「効いていない」
        // とは見えない(利用者の言葉で言えば「切り替わった」と分かる)
        chosen: [...chosen,
                 { key: "残業繋ぎ", value: pickedName(overtime) }] });
    if (early === null) return;
    body.early = early === NONE ? "" : early.name;
  }

  // **送れなかったら、選んだ内容を持ったまま「もう一度」を出す。**
  // 以前は失敗の知らせだけ出て、4つ選び直すしかなかった。札(`submit_id`)は
  // 送り直しても同じ ── 返事だけ届かなかった登録を2回入れない
  body.submit_id = newSubmitId();
  liveRest = `${label} ${worker.name} さんの休み(まだ登録していません)`;
  try {
    for (;;) {
      const result = await submit("/api/rest", body);
      if (result.ok) return;
      const err = result.err;
      if (refusedForGood(err)) {
        // 業務としての断り。**理由はサーバが持っている**ので、そのまま出す。
        // 409(先に変わっていた)は画面が古いので、あわせて読み直す
        await inform("登録できません", err.message);
        if (err.status === 409) await reload();
        return;
      }
      const again = await confirm("まだ登録していません",
        `${explain(err)}\n\n選んだ内容: ${worker.name} さん`
        + (body.shift ? `(${body.shift}直)` : "") + ` ${label} の休み`,
        { okLabel: "もう一度送る", cancelLabel: "やめる" });
      if (!again) return;
    }
  } finally {
    liveRest = null;
  }
}

/** 繋ぎに選ばれたものの見せ方。「未登録のまま」も**選んだ結果**。 */
function pickedName(picked) {
  if (picked === NONE) return "未登録のまま";
  return picked && picked.name ? picked.name : "";
}

/**
 * 作業者を1人選ばせる (tkinter 版 WorkerPickerDialog)。
 *
 * 戻り値の使い分けが要点:
 *   選んだ         … その作業者
 *   「未登録のまま」 … `NONE`(繋ぎを決めないという**選択**)
 *   取り消し        … `null`(流れそのものをやめる)
 *
 * `opts.chosen` / `opts.tag` / `opts.tone` は**見分けるため**にある。
 * 繋ぎでは同じ一覧が続けて2回出るので、これが無いと
 * 「選んだのが効いていない」ように見える(`registerRest`)。
 */
function pickWorker(title, options, opts = {}) {
  return modal({
    title,
    step: opts.step,
    tag: opts.tag,
    tone: opts.tone,
    wide: true,
    render(body, close) {
      // **ここまでに選んだもの。** 直前の選択が出ていれば、
      // 押したことが効いていると分かる
      if (opts.chosen && opts.chosen.length) {
        const done = document.createElement("div");
        done.className = "chosen";
        opts.chosen.forEach((item, i) => {
          const cell = document.createElement("div");
          // **いま増えた1つ**だけ色を付ける。そこが増えたと分かれば
          // 「押したのが効いていない」とは見えない
          cell.className = "chosen__item"
            + (i === opts.chosen.length - 1 ? " chosen__item--last" : "");
          const key = document.createElement("span");
          key.className = "chosen__key";
          key.textContent = item.key;
          const value = document.createElement("b");
          value.className = "chosen__value";
          value.textContent = item.value;
          cell.append(key, value);
          done.appendChild(cell);
        });
        body.appendChild(done);
      }

      const host = document.createElement("div");
      host.className = "workers";
      options.lines.forEach((line) => {
        const section = document.createElement("section");
        section.className = "workers__line";
        const heading = document.createElement("h3");
        heading.textContent = line.line;
        section.appendChild(heading);

        const groups = document.createElement("div");
        groups.className = "workers__groups";
        line.groups.forEach((group) => {
          const column = document.createElement("div");
          column.className = "workers__group";
          const name = document.createElement("h4");
          name.textContent = `${group.group}班`;
          column.appendChild(name);

          const list = document.createElement("ul");
          group.workers.forEach((worker) => {
            const item = document.createElement("li");
            const button = document.createElement("button");
            button.type = "button";
            button.className = "worker";
            button.textContent = worker.name;
            button.addEventListener("click", () => close(worker));
            item.appendChild(button);
            list.appendChild(item);
          });
          column.appendChild(list);
          groups.appendChild(column);
        });
        section.appendChild(groups);
        host.appendChild(section);
      });
      body.appendChild(host);
    },
    actions: opts.allowNone
      // 「未登録のまま」は取り消しではなく**選択**。VBA も同じ扱いで、
      // 繋ぎ担当者に「未登録」を保存する
      ? [{ label: "取り消し", value: null, left: true },
         { label: "未登録のまま", value: NONE, kind: "primary" }]
      : [{ label: "取り消し", value: null }],
  });
}

/* -- コメント・連絡 ------------------------------------------------- */
async function registerComment(date, label) {
  const targets = await api.get("/api/comment-targets");
  if (!targets.lines.length) {
    await inform("宛先がありません",
                 "ライン・班の一覧を取得できませんでした。\n"
                 + "設定画面で班員名簿の取り込みを確認してください。");
    return;
  }

  const chosen = await modal({
    title: `${label} 連絡の宛先`,
    step: "1 / 2",
    render(body, close) {
      const row = document.createElement("div");
      row.className = "row";

      const lineField = field("ライン", "cm-line");
      const groupField = field("班", "cm-group");
      const lineSelect = lineField.querySelector("select");
      const groupSelect = groupField.querySelector("select");

      fill(lineSelect, targets.lines);
      if (targets.my_line && targets.lines.includes(targets.my_line)) {
        lineSelect.value = targets.my_line;
      }
      // 1つ目を選ぶと2つ目が**実在する組み合わせだけ**に絞られる
      const refresh = () => fill(groupSelect, targets.groups[lineSelect.value] || []);
      lineSelect.addEventListener("change", refresh);
      refresh();

      row.append(lineField, groupField);
      body.appendChild(row);

      // Enter でも次へ進める(選ぶだけの画面なので、手が離れない)
      groupSelect.addEventListener("keydown", (event) => {
        if (event.key === "Enter") close({ line: lineSelect.value, group: groupSelect.value });
      });
    },
    actions: [
      { label: "取り消し", value: null },
      { label: "次へ", kind: "primary",
        value: () => {
          const line = document.getElementById("cm-line");
          const group = document.getElementById("cm-group");
          return (line && group && group.value)
            ? { line: line.value, group: group.value } : undefined;
        } },
    ],
  });
  if (!chosen) return;

  await writeComment({ date, label, line: chosen.line, group: chosen.group,
                       text: "", submit_id: newSubmitId() });
}

/* -- 連絡の打ちかけ ---------------------------------------------------
   【何が起きていたか】
   本文を打って「登録する」を押すと、**送る前にダイアログを閉じていた。**
   送れなかったとき(通信の失敗・取り込みの最中・起動し直し)は知らせが
   出るだけで、打った本文はもうどこにも無かった。背景をうっかり押す・
   Esc を押す・画面が読み込み直される、でも黙って消えた。
   「打った行が消えることがありました」── とんでもない話である。

   【どうするか】
   * 打っているあいだ、本文を `sessionStorage` に置く(読み込み直しても戻る。
     タブごとに別なので、別の画面へは漏れない)
   * 送れなかったら、**同じ本文のままダイアログを開き直し**、理由を上に出す
   * 本文がある画面は、閉じる前に訊く(`modal.js` の `dirty`)
   * 送り直しは同じ札(`submit_id`)で送る。返事だけ届かなかった連絡を
     2行にしない。本文を直したら別の送信なので、札を付け直す
   --------------------------------------------------------------------- */
const DRAFT_KEY = "calendar.commentDraft";
// いま打っている(か、送っている)連絡。`leave.js` に「保存していない」と言う
let liveDraft = null;
// 送れずに「もう一度」を待っている休みの登録(説明の文)
let liveRest = null;

function saveDraft(draft) {
  liveDraft = draft;
  try {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch (_err) { /* 使えないブラウザでは画面の中だけで持つ */ }
}

function dropDraft() {
  liveDraft = null;
  try { sessionStorage.removeItem(DRAFT_KEY); } catch (_err) { /* 同上 */ }
}

function storedDraft() {
  try {
    const draft = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "null");
    return draft && draft.date && draft.line && draft.group ? draft : null;
  } catch (_err) {
    return null;
  }
}

/** 読み込み直す前に打っていた連絡があれば、開き直して戻す。 */
async function restoreDraft() {
  const draft = storedDraft();
  if (!draft || !String(draft.text || "").trim()) { dropDraft(); return; }
  // **使っていない画面(2枚目のタブ)では戻さない**(使っている画面のもの)
  const block = document.getElementById("screen-block");
  if (block && !block.hidden) return;
  if (document.querySelector(".modal")) return;
  await writeComment(draft,
    "読み込み直す前に打っていた連絡を戻しました。内容を確かめて「登録する」を押してください。");
}

async function writeComment(draft, firstNote = "") {
  let note = firstNote;
  for (;;) {
    saveDraft(draft);
    const text = await modal({
      title: `${draft.label} ${draft.line} ${draft.group}班 への連絡`,
      step: "2 / 2",
      // 本文があれば、閉じる前に訊く
      dirty: () => {
        const area = document.getElementById("cm-text");
        return !!(area && area.value.trim());
      },
      render(body) {
        if (note) {
          const why = document.createElement("div");
          why.className = "note note--warn";
          why.id = "cm-note";
          why.style.whiteSpace = "pre-wrap";
          why.textContent = note;
          body.appendChild(why);
        }
        const area = document.createElement("textarea");
        area.className = "textarea";
        area.id = "cm-text";
        area.placeholder = "連絡の内容を入力してください";
        area.setAttribute("data-autofocus", "1");
        area.value = draft.text || "";
        // 打つたびに置いておく(読み込み直し・タブの入れ替えに備える)
        area.addEventListener("input", () => {
          draft.text = area.value;
          saveDraft(draft);
        });
        body.appendChild(area);
      },
      actions: [
        { label: "取り消し", value: null },
        { label: "登録する", kind: "primary",
          value: () => {
            const area = document.getElementById("cm-text");
            return area && area.value.trim() ? area.value.trim() : undefined;
          } },
      ],
    });
    if (!text) { dropDraft(); return; }

    // 前に送った本文と違えば別の送信。札を付け直す(同じ札だと、
    // サーバは「もう受けた」として直した本文を書かない)
    if (draft.sent !== undefined && draft.sent !== text) draft.submit_id = newSubmitId();
    draft.text = text;
    draft.sent = text;
    saveDraft(draft);

    const result = await submit("/api/comment", {
      date: draft.date, line: draft.line, group: draft.group, text,
      submit_id: draft.submit_id });
    if (result.ok) { dropDraft(); return; }
    // **本文を持ったまま開き直す。** 理由はダイアログの上に出す
    note = explain(result.err);
  }
}

/** 送信1回ごとの札。送り直しても同じ札を使う(サーバが二重に書かない)。 */
function newSubmitId() {
  if (window.crypto && window.crypto.randomUUID) return crypto.randomUUID();
  return `c-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** 送り直しても通らない断りか(業務としての断り)。 */
function refusedForGood(err) {
  return err instanceof ApiError && (err.status === 409 || err.status === 422);
}

/** 送れなかった理由を、入力を残したことと一緒に言う。 */
function explain(err) {
  if (err instanceof ApiError && err.status && err.status !== 500) {
    // サーバの断り(使用中・起動し直し・業務の断り)。理由はサーバが持っている
    return err.message;
  }
  return `送れませんでした(${err && err.message ? err.message : String(err)})。`
    + "入力はそのまま残してあります。つながりを確かめて、もう一度「登録する」を押してください。";
}

function field(label, id) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const caption = document.createElement("label");
  caption.setAttribute("for", id);
  caption.textContent = label;
  const select = document.createElement("select");
  select.className = "select";
  select.id = id;
  wrap.append(caption, select);
  return wrap;
}

function fill(select, values) {
  select.replaceChildren(...values.map((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    return option;
  }));
}

/* -- 内容閲覧 ------------------------------------------------------- */
async function viewDay(date) {
  const day = await api.get(`/api/day/${date}`);
  await modal({
    title: `${day.title} の登録内容`,
    wide: true,
    render(body) {
      const box = document.createElement("div");
      if (day.items.length) {
        box.className = "viewer";
        box.textContent = day.detail;
      } else {
        box.className = "viewer viewer--empty";
        box.textContent = "この日に登録データはありません。";
      }
      body.appendChild(box);
    },
    actions: [{ label: "閉じる", value: true, kind: "primary" }],
  });
}

/* -- 削除 ----------------------------------------------------------- */
async function deleteDay(date, label) {
  const day = await api.get(`/api/day/${date}`);
  if (!day.items.length) {
    await inform("削除するものがありません", "この日に登録データはありません。");
    return;
  }

  const ids = await modal({
    title: `${label} 削除するものを選んでください`,
    wide: true,
    render(body) {
      const list = document.createElement("div");
      list.className = "picklist";
      list.id = "del-list";
      day.items.forEach((item) => {
        const row = document.createElement("label");
        row.className = "pick";
        row.dataset.kind = item.kind;

        const check = document.createElement("input");
        check.type = "checkbox";
        check.value = String(item.id);

        const text = document.createElement("span");
        text.className = "pick__text";
        text.textContent = item.caption;
        const meta = document.createElement("small");
        meta.className = "pick__meta";
        meta.textContent = [item.line, item.group && `${item.group}班`]
          .filter(Boolean).join(" ");
        text.appendChild(meta);

        row.append(check, text);
        list.appendChild(row);
      });
      body.appendChild(list);
    },
    actions: [
      { label: "取り消し", value: null },
      { label: "選んだものを削除", kind: "danger",
        value: () => {
          const checked = [...document.querySelectorAll("#del-list input:checked")];
          return checked.length ? checked.map((box) => Number(box.value)) : undefined;
        } },
    ],
  });
  if (!ids) return;

  // **取り消せない操作なので、止める確認を残す。**
  // 削除内容は履歴に残るが、カレンダーからは消える
  const ok = await confirm("削除の確認",
    `チェックした ${ids.length} 件を削除します。\n`
    + "(削除内容は履歴に記録されます)",
    { okLabel: "削除する", danger: true });
  if (!ok) return;

  // **画面が見ていた中身も返す。** 開いているあいだに取り込み直しが走ると
  // 同じIDが別の行になっていることがある。合っているかはサーバが見る
  // (印の意味はこちらでは解かない)
  const marks = {};
  day.items.forEach((item) => { marks[item.id] = item.mark; });

  const result = await submit("/api/delete", { date, ids, marks });
  if (result.ok) return;
  const err = result.err;
  if (refusedForGood(err)) {
    await inform("削除できません", err.message);
    if (err.status === 409) await reload();
    return;
  }
  toastError(err);
}

/* ------------------------------------------------------------------
   送信
   ------------------------------------------------------------------ */
/**
 * 送って、返ってきた月を描く。**断られても投げない** ── `{ok, err}` を返し、
 * 入力をどう残すかは呼んだ側が決める(連絡なら本文を持ったまま開き直す)。
 */
async function submit(path, body) {
  // 登録より前に頼んでいた月の読み込みの返事は捨てる(登録した行が
  // 入っていない中身で、登録の結果を上書きさせない)
  const mine = ++loadSeq;
  submitting += 1;
  try {
    const payload = await api.post(path, body);
    // 送っているあいだに利用者が別の月へ移っていたら、そちらを描いたままにする
    if (mine === loadSeq) render(payload);
    else sync.apply(payload.sync);
    toast(payload.message, "ok");
    return { ok: true, payload };
  } catch (err) {
    return { ok: false, err };
  } finally {
    submitting -= 1;
    if (mine === loadSeq) loading = false;
  }
}

/* ------------------------------------------------------------------
   印刷 (tkinter 版 on_print)
   ------------------------------------------------------------------ */
function openPrint() {
  if (!view) return;
  // 別タブで開く。**このタブの心拍を止めない**ため
  // (同じタブで移ると、戻るまでサーバは「誰も見ていない」と数え始める)
  // デスクトップ版は外枠が別の窓で開く(`desktop.js`)。ブラウザ版は別タブ
  openWindow(tokenUrl(`/print?year=${view.year}&month=${view.month}`),
             `${view.year}年${view.month}月 印刷`);
}
