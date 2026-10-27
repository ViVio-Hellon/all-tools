// 確かめの窓(「この資材は届いていますか？」など)を、画面の中に出す
//
// **ブラウザ標準の確かめの窓(confirm・alert・prompt)は使わない。** 標準の窓を
// 出しているあいだは、画面のタイマーが全部止まる ── 心拍(`/api/alive`)も止まる。
// サーバは心拍が 20 秒途切れた画面を一覧から外し(`kanban/screen.py`)、そこから
// 90 秒心拍が無いと「誰も見ていない」と読んで終わる(`kanban/idle_exit.py`)。
// 実際に、窓を出したまま手を止めると 91 秒でサーバ(ブラウザ版)が終わり、
// あとで「OK」を押すと「繋がりません」が出た。
//
// ここで出す窓は画面の一部なので、出しているあいだも心拍・盤の読み直しは続く。
// 標準の窓と同じ約束にそろえる:
//
//   * 答えるまで後ろは押せない(覆いで隠す)。後ろのキー操作も届かせない
//   * Enter で決める(焦点のあるボタン)・Esc でやめる
//   * 開いたら既定のボタンへ焦点を移し、閉じたら**開く前に居た場所へ戻す**
//   * 消す・捨てるなど危ない操作は「やめる」を既定にする(`danger`)。
//     Enter を続けて押しただけで消えないように
//   * 開いた直後の続けざまの2打目(ダブルクリックの2回目)は受けない ── 升を
//     ダブルクリックすると、2回目がちょうど出てきた窓のボタンに当たってしまう
//
// 待ち方は Promise。呼ぶ側は `if (!(await ask(...))) return;` と書く。
//
// デスクトップ版の外枠は、標準の確かめの窓を「答えを待つ」ものに固定している
// (`src-tauri/src/native_dialogs.js`。固定しないと confirm が訊かずに「はい」に
// なる)。ここは標準の窓を使わないので、その固定とはぶつからない。

/** 開いた直後に、続けざまの2打目を受けない時間 (ms) */
const SETTLE_MS = 400;

// 開いている窓の重なり(後に開いたものほど上)。キーを受けるのは一番上だけ
const stack = [];

/**
 * 「はい / やめる」を訊く(標準の confirm の代わり)。
 *
 * @param {string} message 訊くこと(改行はそのまま出す)
 * @param {object} [opts]
 * @param {string} [opts.ok]      決めるボタンの文言
 * @param {string} [opts.cancel]  やめるボタンの文言
 * @param {boolean} [opts.danger] 危ない操作(赤いボタン・「やめる」が既定)
 * @returns {Promise<boolean>} 決めたら true。やめた(Esc を含む)なら false
 */
export function ask(message, { ok = 'OK', cancel = 'やめる', danger = false } = {}) {
  return open(message, { ok, cancel, danger });
}

/**
 * 知らせるだけ(標準の alert の代わり)。閉じたら解決する。
 * @param {string} message
 * @param {object} [opts] `ok`: 閉じるボタンの文言
 * @returns {Promise<void>}
 */
export async function tell(message, { ok = '閉じる' } = {}) {
  await open(message, { ok, cancel: '', danger: false });
}

/** いま確かめの窓を出しているか */
export function isAsking() {
  return stack.length > 0;
}

/**
 * この窓より上に出るもの(「この画面では操作できません」の覆い。`app.js`)の中か。
 * そこはこの窓の答えより先に押してもらうものなので、キーも焦点も奪わない
 */
function above(el) {
  return !!(el && el.closest && el.closest('#screen-blocked'));
}

function open(message, { ok, cancel, danger }) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    const n = stack.length + 1;

    const back = document.createElement('div');
    back.className = 'modal ask';

    const box = document.createElement('div');
    box.className = 'modal__box ask__box';
    box.setAttribute('role', 'alertdialog');
    box.setAttribute('aria-modal', 'true');
    const msg = document.createElement('p');
    msg.className = 'ask__msg';
    msg.id = `ask-msg-${n}`;
    msg.textContent = String(message ?? '');
    box.setAttribute('aria-describedby', msg.id);
    box.appendChild(msg);

    const actions = document.createElement('div');
    actions.className = 'modal__actions';
    let no = null;
    if (cancel) {
      no = document.createElement('button');
      no.type = 'button';
      no.className = 'btn';
      no.dataset.ask = 'no';
      no.textContent = cancel;
      actions.appendChild(no);
    }
    const yes = document.createElement('button');
    yes.type = 'button';
    yes.className = `btn ${danger ? 'btn--danger' : 'btn--primary'}`;
    yes.dataset.ask = 'yes';
    yes.textContent = ok;
    actions.appendChild(yes);
    box.appendChild(actions);
    back.appendChild(box);

    // 既定のボタン(Enter で押されるもの)。危ない操作は「やめる」
    const first = danger && no ? no : yes;
    first.dataset.default = '1';
    const buttons = [no, yes].filter(Boolean);

    const self = { back };
    let done = false;
    function finish(answer) {
      if (done) return;
      done = true;
      ['keydown', 'keyup', 'keypress'].forEach((t) => window.removeEventListener(t, onKey, true));
      window.removeEventListener('focusin', onFocus, true);
      back.remove();
      const at = stack.indexOf(self);
      if (at >= 0) stack.splice(at, 1);
      // 開く前に居た場所へ戻す(戻さないと、次のタブ移動が画面の先頭からになる)
      if (previous && previous.isConnected && previous.focus) {
        try { previous.focus({ preventScroll: true }); } catch (e) { /* 焦点を持てない */ }
      }
      resolve(answer);
    }

    yes.addEventListener('click', () => finish(true));
    if (no) no.addEventListener('click', () => finish(false));

    // **後ろのキー操作は届かせない**(標準の窓と同じ)。窓の中のボタンの既定の動き
    // (Enter・Space で押す)は止めない。下に重なった窓・コメントの窓の Esc も
    // ここで止まる(上の窓だけ閉じる)
    function onKey(ev) {
      // 上に別の窓がある。下の窓は黙る(上の窓が受けて止める)
      if (stack[stack.length - 1] !== self) return;
      if (above(ev.target)) return;
      ev.stopImmediatePropagation();
      if (ev.type !== 'keydown') return;
      const inside = box.contains(ev.target);
      if (ev.key === 'Escape') {
        ev.preventDefault();
        finish(false);
        return;
      }
      if (ev.key === 'Enter') {
        // 押しっぱなしの Enter(前の操作の続き)では決めない
        if (ev.repeat) { ev.preventDefault(); return; }
        if (inside && ev.target.tagName === 'BUTTON') return;   // 焦点のあるボタンが押される
        ev.preventDefault();
        first.click();
        return;
      }
      if (ev.key === 'Tab') {
        // 窓の中だけを回る
        ev.preventDefault();
        const at = buttons.indexOf(document.activeElement);
        const step = ev.shiftKey ? -1 : 1;
        const next = at < 0 ? first : buttons[(at + step + buttons.length) % buttons.length];
        next.focus();
        return;
      }
      if (!inside) ev.preventDefault();
    }
    // 焦点が窓の外へ出たら(覆いを押した・後ろの画面が焦点を取った)既定のボタンへ戻す
    function onFocus(ev) {
      if (stack[stack.length - 1] !== self) return;
      if (!box.contains(ev.target) && !above(ev.target)) first.focus();
    }
    ['keydown', 'keyup', 'keypress'].forEach((t) => window.addEventListener(t, onKey, true));
    window.addEventListener('focusin', onFocus, true);

    // 覆いを押しても閉じない(標準の窓と同じ。うっかり押して答えたことにしない)。
    // 焦点も窓から動かさない
    back.addEventListener('mousedown', (ev) => {
      if (!box.contains(ev.target)) ev.preventDefault();
    });

    // 開いた直後の続けざまの2打目は受けない(升のダブルクリックの2回目が、
    // 出てきたばかりの窓のボタンに当たらないように)。1打目はそのまま通す
    const openedAt = performance.now();
    const swallow = (ev) => {
      if (ev.detail < 2) return;
      if (performance.now() - openedAt >= SETTLE_MS) return;
      ev.preventDefault();
      ev.stopImmediatePropagation();
    };
    ['mousedown', 'mouseup', 'click', 'dblclick'].forEach((t) => back.addEventListener(t, swallow, true));

    stack.push(self);
    document.body.appendChild(back);
    first.focus();
  });
}
