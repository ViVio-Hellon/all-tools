/*
  modal.js — 止める確認と、順に選ばせる画面

  tkinter 版のダイアログ(PickerDialog / WorkerPickerDialog /
  ComboPickerDialog / ViewerDialog / DeletePickerDialog / ask_text)を
  ここ1つで受ける。

  **選択肢の中身はサーバが持つ。** ここが持つのは
  「開く・閉じる・選ばれたものを返す」だけ。

  約束:
  * `Esc` と背景クリックで閉じる(= 取り消し)。tkinter 版のキャンセルと同じ
  * 開いたら中の最初の要素へ焦点を移す。閉じたら**開く前に居た場所へ戻す**
  * 開いているあいだは後ろをタブ移動させない
  * **開いた直後のひと押しは受けない**(下記)
*/

let openCount = 0;

/**
 * 開いた直後に、**続けざまの2打目**を受けない時間 (ms)。
 *
 * **同じ形の画面が続けて出るため。** 休みの登録は
 * 「休む本人 → 直 → 残業繋ぎ → 早出繋ぎ」と進み、作業者を選ぶ画面が
 * 同じ場所に同じ並びで2回出ます。ここでダブルクリックすると、
 * 1回目で残業の画面が閉じ、**2回目が早出の画面の同じ位置に届いて**
 * しまいます ── 早出の画面は一瞬出て消え、押した覚えのない人が
 * 早出繋ぎとして保存されます。
 *
 * 画面から見えるのは「残業で終わってしまった」。Access のころの癖で
 * 一覧はダブルクリックで選ぶ人が多いので、現場では普通に起きます。
 *
 * **止めるのは2打目だけです**(`event.detail >= 2`)。時間だけで切ると、
 * 速く押しただけの人の1打目まで黙って消えます ── このツールで一番
 * 避けたい「押しても何も起きない」そのものになります。
 */
const SETTLE_MS = 400;

/**
 * 「選ばない」ことを選んだ、という値。
 *
 * 取り消し(`null`)と区別が要る場面がある ── 繋ぎ担当者の
 * 「未登録のまま」は**選択**であって、流れをやめたわけではない
 * (VBA も「未登録」という値を保存していた)。
 */
export const NONE = Symbol("none");

/**
 * モーダルを1枚開く。閉じたときの値で解決する Promise を返す。
 *
 * @param {object} spec
 * @param {string} spec.title           見出し
 * @param {string} [spec.step]          「2 / 4」のような進み具合
 * @param {(body:HTMLElement, close:(value:any)=>void) => void} spec.render
 *        中身を組み立てる。`close(値)` を呼ぶと、その値で解決する
 * @param {Array<{label:string, kind?:string, value?:any, primary?:boolean}>} [spec.actions]
 *        下端のボタン。押すと `value` で解決する
 * @param {boolean} [spec.wide]         広い箱(削除・閲覧・作業者選択)
 * @param {string} [spec.tag]           見出しの前に出す札(「残業繋ぎ」など)
 * @param {string} [spec.tone]          札の色みの区別 (`overtime` / `early`)
 * @returns {Promise<any>} 取り消しなら `null`
 */
export function modal(spec) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    const back = document.createElement("div");
    back.className = "modal";

    const box = document.createElement("div");
    // `tone` は**箱ごと色を変える**(帯・上端・札・段)。
    // 同じ形の画面が続けて出るときに、見出しを読まずに見分けるため
    box.className = "modal__box"
      + (spec.wide ? " modal__box--wide" : "")
      + (spec.tone ? ` modal__box--toned modal__box--tone-${spec.tone}` : "");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", spec.title || "");

    // -- 見出し ----------------------------------------------------
    const head = document.createElement("div");
    head.className = "modal__head";
    // 札。**同じ形の画面が続けて出るときに、見出しの手前で見分ける**
    // (繋ぎは残業と早出を続けて聞く ── `views/calendar.js`)
    if (spec.tag) {
      const tag = document.createElement("span");
      tag.className = "modal__tag"
        + (spec.tone ? ` modal__tag--${spec.tone}` : "");
      tag.textContent = spec.tag;
      head.appendChild(tag);
    }
    const title = document.createElement("h2");
    title.textContent = spec.title || "";
    head.appendChild(title);
    if (spec.step) {
      const step = document.createElement("span");
      step.className = "modal__step";
      step.textContent = spec.step;
      head.appendChild(step);
    }
    box.appendChild(head);

    // -- 中身 ------------------------------------------------------
    const body = document.createElement("div");
    body.className = "modal__body";
    box.appendChild(body);

    // -- 下端 ------------------------------------------------------
    let foot = null;
    if (spec.actions && spec.actions.length) {
      foot = document.createElement("div");
      foot.className = "modal__foot";
      spec.actions.forEach((action) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn" + (action.kind ? ` btn--${action.kind}` : "");
        button.textContent = action.label;
        if (action.left) button.classList.add("left");
        button.addEventListener("click", () => {
          // 値は関数でも渡せる(入力欄の中身を読むため)。
          // **関数が `undefined` を返したら閉じない** ── 入力が足りない
          // ことを意味する(「次へ」を押したが班を選んでいない、など)。
          // 直値の場合は必ず閉じる
          if (typeof action.value !== "function") {
            close(action.value);
            return;
          }
          const value = action.value();
          if (value !== undefined) close(value);
        });
        foot.appendChild(button);
      });
      box.appendChild(foot);
    }

    back.appendChild(box);

    // -- 開閉 ------------------------------------------------------
    let closed = false;
    function close(value) {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", onKey, true);
      back.remove();
      openCount -= 1;
      if (openCount === 0) document.body.style.removeProperty("overflow");
      // **開く前に居た場所へ焦点を戻す。** 戻さないと、閉じたあとの
      // タブ移動が画面の先頭からになる
      if (previous && previous.focus) previous.focus();
      // 取り消し(Esc・背景・取り消しボタン)は必ず `null`。
      // 呼ぶ側は `null` だけを見れば「やめた」と分かる
      resolve(value === undefined ? null : value);
    }

    function onKey(event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        close(null);
        return;
      }
      if (event.key !== "Tab") return;
      // 開いているあいだは後ろをタブ移動させない
      const focusable = box.querySelectorAll(
        'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])');
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    // -- 前の画面への2打目を、この画面に届かせない ----------------------
    // 捕捉(capture)で受けて、中のボタンより先に止める。
    // **止めるのは続けざまの2打目だけ**(`detail >= 2`)なので、
    // 速く押しただけの1打は普通に通る(`SETTLE_MS` の説明)。
    const openedAt = performance.now();
    function swallow(event) {
      if (event.detail < 2) return;                 // 押し始めの1打は通す
      if (performance.now() - openedAt >= SETTLE_MS) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }
    ["mousedown", "mouseup", "click", "dblclick"].forEach((name) => {
      back.addEventListener(name, swallow, true);
    });

    back.addEventListener("mousedown", (event) => {
      if (event.target === back) close(null);
    });
    document.addEventListener("keydown", onKey, true);

    spec.render(body, close);

    document.body.appendChild(back);
    openCount += 1;
    document.body.style.overflow = "hidden";

    const target = box.querySelector("[data-autofocus]")
                || body.querySelector("button, input, select, textarea")
                || (foot && foot.querySelector("button"));
    if (target) target.focus();
  });
}

/**
 * 一覧から1つ選ばせる (tkinter 版 PickerDialog)。
 *
 * @param {string} title
 * @param {Array<{label:string, value:any, note?:string}>} choices
 * @param {object} [opts] `step` / `cancelLabel`
 */
export function pick(title, choices, opts = {}) {
  return modal({
    title,
    step: opts.step,
    render(body, close) {
      const list = document.createElement("div");
      list.className = "choices";
      choices.forEach((choice) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "choice";
        const label = document.createElement("span");
        label.textContent = choice.label;
        button.appendChild(label);
        if (choice.note) {
          const note = document.createElement("small");
          note.textContent = choice.note;
          button.appendChild(note);
        }
        button.addEventListener("click", () => close(choice.value));
        list.appendChild(button);
      });
      body.appendChild(list);
    },
    actions: [{ label: opts.cancelLabel || "取り消し", value: null }],
  });
}

/**
 * 取り消せない操作の確認 (tkinter 版 messagebox.askyesno)。
 * @returns {Promise<boolean>}
 */
export async function confirm(title, message, opts = {}) {
  const answer = await modal({
    title,
    render(body) {
      const p = document.createElement("p");
      p.style.whiteSpace = "pre-wrap";
      p.style.lineHeight = "1.7";
      p.textContent = message;
      body.appendChild(p);
    },
    actions: [
      { label: opts.cancelLabel || "やめる", value: false },
      { label: opts.okLabel || "実行する", value: true,
        kind: opts.danger ? "danger" : "primary" },
    ],
  });
  return answer === true;
}

/** ひとこと伝えるだけ (tkinter 版 messagebox.showinfo)。 */
export function inform(title, message) {
  return modal({
    title,
    render(body) {
      const p = document.createElement("p");
      p.style.whiteSpace = "pre-wrap";
      p.style.lineHeight = "1.7";
      p.textContent = message;
      body.appendChild(p);
    },
    actions: [{ label: "閉じる", value: true, kind: "primary" }],
  });
}
