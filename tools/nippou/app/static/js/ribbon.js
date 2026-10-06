/*
  ribbon.js — 帯(報告日・ライン・直・ページ)を塗り直す

  【なぜ要るのか】
  帯はサーバが `base.html` に描いたまま、**画面を移るまで更新されません
  でした。** 日報入力を開いたまま 17:00 をまたぐと、保存先だけが 2直に
  移るのに、帯は「1直」と言い続けます ──
  **画面が嘘をつきながら別の直へ書いている**状態になります。

  そこでサーバは応答のたびに `ribbon` を返し(`app/routes/entry.py:_view`)、
  ここがそのとおりに塗ります。**判断はしません** ── 何を出すかを決めるのは
  `work_context.WorkContext.ribbon` ただ1つです。

  【変わったところを目で追えるようにする】
  黙って書き換えると、見ていた人には気づけません。値が変わった枠だけ
  `data-changed` を数秒立てて、そこに目が行くようにします。
*/

/** 値が変わった印を立てておく長さ(ms)。 */
const FLASH_MS = 6000;

const SLOTS = [
  ["rb-date", "report_date"],
  ["rb-line", "line"],
  ["rb-shift", "shift"],
  ["rb-page", "page"],
];

function paintSlot(id, value) {
  const el = document.getElementById(id);
  if (!el || value === undefined || value === null) return false;
  const next = String(value);
  if (el.textContent === next) return false;
  el.textContent = next;
  // 空欄は読めないので「—」を出す決まり。印もそれに合わせる
  if (next === "—") el.setAttribute("data-empty", "1");
  else el.removeAttribute("data-empty");
  el.dataset.changed = "1";
  setTimeout(() => { delete el.dataset.changed; }, FLASH_MS);
  return true;
}

/*
  入った印は、**そのまま押して出られる**。

  「過去データの終了方法 / 管理者モードの終了方法 これらがわからない」と
  言われたところです。どちらも帯に「いま入っている」ことは出していました
  が、やめる場所は別の画面の奥にありました(呼出は日報入力の下のほう、
  管理者モードは設定・管理者の「この端末」)。状態の隣に出口を置きます。

  押せないもの(「いま 2直」など `action` の無いもの)は `<span>` のまま。
*/
function paintChips(chips) {
  const box = document.getElementById("rb-chips");
  if (!box || !Array.isArray(chips)) return;
  const next = chips.map((c) => `${c.kind}:${c.text}:${c.action || ""}`).join("|");
  if (box.dataset.shown === next) return;
  box.dataset.shown = next;
  box.replaceChildren();
  for (const chip of chips) {
    const node = document.createElement(chip.action ? "button" : "span");
    node.className = `chip chip--${chip.kind}${chip.action ? " chip--act" : ""}`;
    if (chip.title) node.title = chip.title;
    if (chip.action) {
      node.type = "button";
      node.dataset.chipAction = chip.action;
      if (chip.confirm) node.dataset.chipConfirm = chip.confirm;
      node.textContent = chip.text;
      const x = document.createElement("span");
      x.className = "chip__x";
      x.setAttribute("aria-hidden", "true");
      x.textContent = "✕";
      node.appendChild(x);
    } else {
      node.textContent = chip.text;
    }
    box.appendChild(node);
  }
}

/**
 * 印を押したときの動き。**`app.js` が1度だけ繋ぎます**(帯は差し替えの
 * 対象外なので、画面を移っても付いたまま)。
 *
 * どちらも**確かめてから**にします ── 押し間違いで過去データを閉じると、
 * 直していた内容がどこへ行ったのか分からなくなります(実際には
 * 閉じる前に保存しますが、押した人にはそう見えません)。
 */
export function wireChips(box, { back, adminOff }) {
  box?.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-chip-action]");
    if (!chip) return;
    // 確かめの文言は印が持っている(同じ直のページ直しと過去データで違う)
    if (chip.dataset.chipAction === "back") back?.(chip.dataset.chipConfirm || "");
    if (chip.dataset.chipAction === "admin-off") adminOff?.();
  });
}

/**
 * 「直が変わりました」の帯を出す/消す。**トーストにはしない。**
 *
 * 数秒で消えるものにすると、打つ手を止めて離れているあいだに出て、
 * 戻ったときには消えています ── いちばん気づかせたい場面が、いちばん
 * 見落とされます。文言はサーバのもの(`logic/shift_boundary.py`)。
 *
 * 出す側は2つあります(1分ごとの見張りと、見送られた自動保存の応答)。
 * **同じ帯を同じ関数で動かす** ── 2つが別々に出し入れすると、片方が
 * 消した直後にもう片方が出す、という点滅になります。
 *
 * @param {string} text 空文字なら消す
 */
export function showShiftMoved(text) {
  const box = document.getElementById("shift-moved");
  const node = document.getElementById("shift-moved-text");
  if (!box || !node) return;
  if (!text) { box.hidden = true; return; }
  if (node.textContent !== text) node.textContent = text;
  box.hidden = false;
}

/**
 * サーバが返した帯を写す。
 *
 * @param {object} data `{report_date, line, shift, page, chips, warn, titles}`
 * @returns {boolean} 1つでも変わったか
 */
export function paintRibbon(data) {
  if (!data) return false;
  let moved = false;
  for (const [id, key] of SLOTS) {
    if (paintSlot(id, data[key])) moved = true;
    paintWarn(id, key, data);
  }
  paintChips(data.chips);
  return moved;
}

/**
 * 警告の色で出す枠(ラインを決めていない端末の「未設定」など)。
 * **どれを警告にするかはサーバが決める**(`warn` と `titles`)。
 */
function paintWarn(id, key, data) {
  const el = document.getElementById(id);
  if (!el || !Array.isArray(data.warn)) return;
  const warn = data.warn.includes(key);
  if (warn) el.setAttribute("data-warn", "1");
  else el.removeAttribute("data-warn");
  const title = (data.titles || {})[key] || "";
  if (title) el.title = title;
  else el.removeAttribute("title");
}
