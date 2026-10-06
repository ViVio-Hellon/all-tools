// 看板の描画と操作
//
// **業務判断はしない。** 押せるか・何色か・何と出すかはサーバが決めて
// ビューモデルに入れてくる(`kanban/presenters/board.py`)。ここがするのは
// 「ビューモデル → DOM」と「押された → サーバへ送る → 返ってきた盤面で描き直す」
// の2つだけ。
//
// **操作のたびに盤面ぜんぶを描き直す。** 押したボタンだけを書き換えると、
// 1回の操作で他の列も動く場合(注文中を押すと発送が消える等)に画面と
// サーバがずれる。差分を持たないほうが、この規模では確実で速い。

import { api, withLine } from '../api.js';
import { bad, ok, warn } from '../toast.js';

let line = '';
let busy = false;
let alerts = [];         // 「発送処理中に注文が取り消されました」(サーバが決めて渡す)
let canComment = false;  // この画面でコメントを書けるか(倉庫参照は読むだけ)
let commentSide = '';    // 書くときの名乗り(現場 / 倉庫)
let openThread = null;   // いま開いているやり取り { line, mgmt_no }
let poll = null;
let lastToken = '';

export function start(initial, initialLine) {
  line = initialLine || '';
  wireTabs();
  wireToolbar();
  wireBoard();
  wireComments();

  if (initial) render(initial);
  else if (line) reload();

  loadReports();
  startPolling();
}

// ------------------------------------------------------------------
// 描画
// ------------------------------------------------------------------
/** いま描いている看板(管理番号 → 行)。押す前にコメントを見せるかを決めるのに使う */
let rowsByNo = new Map();

function render(board) {
  const host = document.getElementById('board');
  if (!host || !board) return;

  rowsByNo = new Map();
  for (const group of board.groups) {
    for (const r of group.rows) rowsByNo.set(String(r.mgmt_no), { ...r, material: group.material });
  }
  host.innerHTML = '';
  for (const group of board.groups) {
    host.appendChild(frame(group));
  }

  const note = document.getElementById('note');
  if (note) note.textContent = board.note || '';

  setCount('c-reset', board.batch_reset_count);
  setCount('c-ship', board.batch_ship_count);
  renderAlerts(board.alerts || []);
  const missing = document.getElementById('table-missing');
  if (missing) { missing.hidden = !board.missing_table; missing.textContent = board.missing_table || ''; }
  canComment = Boolean(board.can_comment);
  commentSide = board.comment_side || '';
  renderCommentAlerts(board.comment_alerts || []);
  // 開いたままのやり取りに新しいコメントが届いたら、その場で出し直す
  if (openThread) loadThread(openThread.line, openThread.mgmt_no, { quiet: true });

  if (!board.groups.length) {
    host.innerHTML =
      '<div class="empty">このラインに表示する資材がありません。</div>';
  }
}

function frame(group) {
  const el = document.createElement('section');
  el.className = 'frame';

  const title = document.createElement('div');
  title.className = 'frame__title';
  title.textContent = group.material;
  el.appendChild(title);

  const rows = document.createElement('div');
  rows.className = 'frame__rows';
  for (const r of group.rows) rows.appendChild(row(r));
  el.appendChild(rows);
  return el;
}

function row(r) {
  const el = document.createElement('div');
  el.className = 'row' + (r.locked ? ' row--locked' : '') + (r.alert ? ' row--alert' : '');
  if (r.alert) el.title = r.alert;
  el.dataset.no = r.mgmt_no;
  el.dataset.rev = r.rev;

  el.appendChild(cell(r.size, r, 'size', r.non_permanent));
  el.appendChild(cell(r.ship, r, 'ship', false));
  el.appendChild(cell(r.hold, r, 'hold', false));
  el.appendChild(commentCell(r));
  return el;
}

/** 💬 ボタン。数はいまのやり取りの件数、赤い点は未読 */
function commentCell(r) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = 'cell cell--comment' + (r.comments ? ' has-comments' : '') + (r.unread ? ' is-unread' : '');
  el.dataset.kind = 'comment';
  el.textContent = r.comments ? `💬${r.comments}` : '💬';
  el.title = r.comments
    ? `コメント ${r.comments} 件${r.unread ? `(未読 ${r.unread} 件)` : ''}`
    : 'コメントを書く・読む';
  el.setAttribute('aria-label', el.title);
  return el;
}

function cell(view, r, kind, nonPermanent) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = `cell cell--${kind}`;
  if (nonPermanent) el.classList.add('cell--non-permanent');
  // 押せないものはラベルとして見せる(押せる見た目にしない)
  if (!view.enabled) el.classList.add('cell--label');
  el.dataset.state = view.state;
  el.dataset.kind = kind;
  el.disabled = !view.enabled;
  el.textContent = view.label;

  // 錠前の行は「なぜ押せないか」をホバーで読めるようにする。
  // 押しても何も起きないだけだと、壊れているようにしか見えない
  const tip = r.locked && kind === 'size' ? r.locked_note : view.tip;
  if (tip) el.title = tip;
  return el;
}

function setCount(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = n;
  // 0 件なら押しても何も起きない。押す前に分かるようにボタンごと止める
  const btn = el.closest('button');
  if (btn) btn.disabled = !n;
}

function applyPayload(payload) {
  if (!payload) return;
  if (payload.board) render(payload.board);
  if ('pending' in payload) setPending(payload.pending, payload.failures);
  if ('presence' in payload) setOpenTerminals(payload.presence);
}

function setPending(n, failures) {
  const el = document.getElementById('rb-pending');
  if (!el) return;
  el.textContent = failures ? `${n} (要確認 ${failures})` : String(n);
  el.style.color = failures ? 'var(--bad)' : '';
}

// いま開いている他の端末を帯に出す(VBA の Form状態管理)。
//
// 倉庫が確認している最中に現場が開いていれば、**これから看板が出るかも
// しれない**と身構えられる。VBA 版は自分でダブルクリックしないと分から
// なかったが、ここは帯の定期取得に相乗りするので押さなくても変わる。
//
// **返事が来なくなっただけの相手を「開いている」と言わない。** 異常終了で
// 印が立ったままの相手は `stale` として別に見せる ── 一度でも嘘をつくと、
// この表示は二度と信じてもらえない。
function setOpenTerminals(list) {
  const slot = document.getElementById('rb-open-slot');
  const el = document.getElementById('rb-open');
  if (!slot || !el) return;

  const rows = list || [];
  if (!rows.length) { slot.hidden = true; el.textContent = ''; return; }

  slot.hidden = false;
  el.innerHTML = '';
  for (const p of rows) {
    const chip = document.createElement('span');
    chip.className = 'who' + (p.stale ? ' who--stale' : '');
    chip.textContent = p.label + (p.stale ? '?' : '');
    chip.title = p.stale
      ? `${p.label} は開いた印が残っていますが、${Math.round(p.quiet_sec / 60)} 分ほど`
        + `更新がありません(異常終了の可能性)。最後の更新: ${p.stamp || '不明'}`
      : `${p.label} が開いています${p.host ? `(${p.host})` : ''}。最後の更新: ${p.stamp || '不明'}`;
    el.appendChild(chip);
  }
}

// ------------------------------------------------------------------
// 操作
// ------------------------------------------------------------------
// 発送処理中に注文が取り消されました(赤なし・緑あり)
// ------------------------------------------------------------------
// 倉庫が発送したのを、この端末が取り込む前に赤を消すと起きる。倉庫は物を
// 出しているので、**現場に確かめてもらうまで帯を出し続ける**。消えるトースト
// にしない(見落とすと、発送した物の行方が分からなくなる)。
function renderAlerts(list) {
  alerts = list;
  markTabs(list);
  const box = document.getElementById('alerts');
  if (!box) return;
  box.innerHTML = '';
  box.hidden = !list.length;
  if (!list.length) return;

  const head = document.createElement('b');
  head.textContent = `⚠ ${list[0].message}(${list.length} 件)`;
  const note = document.createElement('p');
  note.textContent = list.some((a) => a.can_acknowledge)
    ? '倉庫はすでに発送しています。資材を確かめてから「確認した」を押してください。'
    : '倉庫は発送しましたが、現場が注文を取り消しています。現場の確認待ちです。';
  box.append(head, note);

  // 倉庫は全ラインぶん出る。ラインの名前を付け、別のラインならそこへ開ける
  // (現場はラインが 1 本なのでタブが無く、名前も要らない)
  const withLineName = Boolean(document.getElementById('line-tabs'));
  const ul = document.createElement('ul');
  for (const a of list) {
    const li = document.createElement('li');
    const text = document.createElement('span');
    text.textContent = (withLineName ? `【${a.line_label}】` : '')
      + `${a.material} ${a.size}(管理番号 ${a.mgmt_no})`
      + (a.shipped_at ? ` ── 発送: ${a.shipped_at}` : '');
    li.append(text);
    if (a.can_acknowledge) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn--sm';
      btn.textContent = '確認した';
      btn.addEventListener('click', () => acknowledge(a));
      li.append(btn);
    } else if (a.line !== line) {
      const tab = document.querySelector(`#line-tabs .tab[data-line="${CSS.escape(a.line)}"]`);
      if (tab) {
        const go = document.createElement('button');
        go.type = 'button';
        go.className = 'btn btn--sm';
        go.textContent = `${a.line_label} を開く`;
        go.addEventListener('click', () => tab.click());
        li.append(go);
      }
    }
    ul.append(li);
  }
  box.append(ul);
}

/** ラインのタブに ⚠ を付ける(どのラインで起きているか、タブを見て分かるように) */
function markTabs(list) {
  const lines = new Set(list.map((a) => a.line));
  for (const tab of document.querySelectorAll('#line-tabs .tab')) {
    tab.classList.toggle('tab--alert', lines.has(tab.dataset.line));
    if (lines.has(tab.dataset.line)) tab.title = '発送処理中に注文が取り消された看板があります';
    else tab.removeAttribute('title');
  }
}

async function acknowledge(a) {
  if (busy) return;
  if (!confirm(`${a.message}。\n\n${a.material} ${a.size}(管理番号 ${a.mgmt_no})\n`
               + '倉庫はすでに発送しています。資材を確かめましたか？\n\n'
               + '[OK] を押すと発送の印(緑)を消します。もう一度必要なら、そのあとで出し直してください。')) return;
  const r = await send('/api/board/acknowledge', { line, mgmt_no: a.mgmt_no, rev: a.rev });
  if (r) ok('確認しました(発送の印を消しました)');
}

// ------------------------------------------------------------------
// コメント(倉庫 ⇔ 現場のやり取り)
// ------------------------------------------------------------------
// 看板ごと。届いた(赤を消した)ら画面からは片付く(共有DBには残る)。
// 相手が書いたものは 💬 に赤い点と、盤面の上の帯で知らせる。開いたら既読。
function renderCommentAlerts(list) {
  const lines = new Set(list.map((a) => a.line));
  for (const tab of document.querySelectorAll('#line-tabs .tab')) {
    tab.classList.toggle('tab--comment', lines.has(tab.dataset.line));
  }
  const box = document.getElementById('comment-alerts');
  if (!box) return;
  box.innerHTML = '';
  box.hidden = !list.length;
  if (!list.length) return;
  const total = list.reduce((n, a) => n + a.unread, 0);
  const head = document.createElement('b');
  head.textContent = `💬 新しいコメント ${total} 件`;
  box.append(head);
  const withLineName = Boolean(document.getElementById('line-tabs'));
  const ul = document.createElement('ul');
  for (const a of list) {
    const li = document.createElement('li');
    const text = document.createElement('span');
    text.textContent = (withLineName ? `【${a.line_label}】` : '')
      + `${a.material} ${a.size}(管理番号 ${a.mgmt_no}) ── ${a.last_side} ${a.last_at}「${a.last_body}」`;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--sm';
    btn.textContent = '開く';
    btn.addEventListener('click', () => openComments(a.line, a.mgmt_no));
    li.append(text, btn);
    ul.append(li);
  }
  box.append(ul);
}

function wireComments() {
  const modal = document.getElementById('comment-modal');
  if (!modal) return;
  const body = document.getElementById('cm-body');
  const close = () => { modal.hidden = true; openThread = null; reload(); };
  document.getElementById('cm-close').addEventListener('click', close);
  modal.addEventListener('click', (ev) => { if (ev.target === modal) close(); });
  document.addEventListener('keydown', (ev) => { if (!modal.hidden && ev.key === 'Escape') close(); });
  document.getElementById('cm-form').addEventListener('submit', (ev) => { ev.preventDefault(); postComment(); });
  document.getElementById('cm-fold').addEventListener('click', () => {
    try { localStorage.setItem(FOLD_KEY, folded() ? '0' : '1'); } catch (e) { /* この画面のあいだだけ */ }
    applyFold();
  });
  body.addEventListener('input', countChars);
  body.addEventListener('keydown', (ev) => {
    // Ctrl+Enter で送る(Enter だけでは送らない ── 改行のつもりで押して送ってしまう)
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); postComment(); }
  });
}

function countChars() {
  const body = document.getElementById('cm-body');
  const max = Number(body.maxLength) || 200;
  document.getElementById('cm-count').textContent = `${body.value.length} / ${max}`;
  document.getElementById('cm-send').disabled = !body.value.trim();
}

async function openComments(l, mgmtNo) {
  openThread = { line: l, mgmt_no: mgmtNo };
  document.getElementById('cm-body').value = '';
  await loadThread(l, mgmtNo, { quiet: false });
  const modal = document.getElementById('comment-modal');
  modal.hidden = false;
  if (canComment) document.getElementById('cm-body').focus();
}

async function loadThread(l, mgmtNo, { quiet }) {
  try {
    const q = new URLSearchParams({ line: l, mgmt_no: mgmtNo, view: line });
    const r = await api.get(`/api/comments?${q}`);
    drawThread(r);
    // 既読にしたので、未読の印を消した盤面をもらう(開いたままの描き直しでは使わない)
    if (!quiet && r.board) applyPayload({ board: r.board });
  } catch (e) {
    if (!quiet) bad(e.message);
  }
}

function drawThread(r) {
  document.getElementById('cm-title').textContent = `💬 ${r.material} ${r.size}`;
  document.getElementById('cm-sub').textContent =
    `${r.line_label} / 管理番号 ${r.mgmt_no} ── ボタンが押されると確認済みの区切りが入り、`
    + '届いたら(赤と緑を消したら)前の回へ畳みます';
  const list = document.getElementById('cm-thread');
  list.innerHTML = '';
  if (!r.open.some((c) => c.kind !== '確認')) {
    const p = document.createElement('p');
    p.className = 'cm-empty';
    p.textContent = 'まだコメントはありません。';
    list.append(p);
  }
  // 届くまでは開いたまま(赤 → 返事 → 黄 → 緑 と続けて読める)。確認済みの分は自分で畳める
  const lastMark = r.open.map((c) => c.kind).lastIndexOf('確認');
  r.open.forEach((c, i) => {
    const el = c.kind === '確認' || c.kind === '片付け' ? closeMark(c) : bubble(c, r.side);
    if (i < lastMark) el.classList.add('cm-confirmed');
    list.append(el);
  });
  const fold = document.getElementById('cm-fold');
  fold.hidden = lastMark < 0;
  applyFold();
  list.scrollTop = list.scrollHeight;

  const hist = document.getElementById('cm-history');
  const closed = document.getElementById('cm-closed');
  closed.innerHTML = '';
  hist.hidden = !r.closed.length;
  const past = r.closed.filter((c) => c.kind !== '片付け').length;
  document.getElementById('cm-history-sum').textContent = `前の回のやり取り(${past} 件)`;
  // 古い順(返事が問いかけの下に来る)。「何をして確認済みになったか」を区切りに出す
  for (const c of r.closed) closed.append(c.kind === '片付け' ? closeMark(c) : bubble(c, r.side));

  const form = document.getElementById('cm-form');
  form.hidden = !r.can_comment;
  document.getElementById('cm-send').hidden = !r.can_comment;
  document.getElementById('cm-send').textContent = `送る(${r.side}として)`;
  document.getElementById('cm-body').maxLength = r.max_len || 200;
  document.getElementById('cm-body').placeholder = r.hint || '';
  // 書けないときは理由(段階が違う)を言う。倉庫参照は「読むだけ」
  const ro = document.getElementById('cm-readonly');
  ro.hidden = r.can_comment;
  ro.textContent = r.cannot_reason || '倉庫参照モードでは読むだけです。';
  countChars();
}

/** 区切り: 何をして確認済みになったか(確認)・前の回へ畳んだか(片付け) */
function closeMark(c) {
  const el = document.createElement('div');
  el.className = 'cm-closemark';
  el.textContent = `── ${c.at.slice(5, 16)} ${c.body}(${c.kind === '片付け' ? 'ここでこの回は終わり' : 'ここまで確認済み'})`;
  return el;
}

// 確認済みの分を畳むか(この端末に覚える。はじめは開いたまま)
const FOLD_KEY = 'kanban.comments.fold';
function folded() {
  try { return localStorage.getItem(FOLD_KEY) === '1'; } catch (e) { return false; }
}
function applyFold() {
  const on = folded();
  const list = document.getElementById('cm-thread');
  list.classList.toggle('is-folded', on);
  const n = list.querySelectorAll('.cm-item.cm-confirmed').length;
  document.getElementById('cm-fold').textContent = on ? `確認済みの ${n} 件を開く` : '確認済みを畳む';
}

function bubble(c, mySide) {
  const el = document.createElement('div');
  el.className = 'cm-item' + (c.side === mySide ? ' is-mine' : '');
  const meta = document.createElement('div');
  meta.className = 'cm-meta';
  meta.textContent = `${c.side || ''} ${c.at.slice(5, 16)}`;
  const text = document.createElement('div');
  text.className = 'cm-text';
  text.textContent = c.body;
  el.append(meta, text);
  // 自分の側が書いたものは、**相手側が読んだか**を添える(既読は側ごとに共有している)
  if (c.side === mySide) {
    const read = document.createElement('div');
    read.className = 'cm-read' + (c.read ? ' is-read' : '');
    read.textContent = c.read
      ? `✓ 既読 ${c.read.at.slice(5, 16)}(${c.read.side} ${c.read.host})`
      : 'まだ読まれていません';
    el.append(read);
  }
  return el;
}

async function postComment() {
  if (!openThread || busy) return;
  const bodyEl = document.getElementById('cm-body');
  const text = bodyEl.value.trim();
  if (!text) return;
  busy = true;
  document.getElementById('cm-send').disabled = true;
  try {
    const r = await api.post('/api/comments', { ...openThread, body: text, view: line });
    bodyEl.value = '';
    drawThread(r);
    if (r.board) applyPayload({ board: r.board });
    ok('コメントを送りました');
  } catch (e) {
    // 開いているあいだに相手が状態を変えて、いまは書けない段階になった
    if (e.code === 'comment_closed' && e.body) {
      drawThread(e.body);
      if (e.body.board) applyPayload({ board: e.body.board });
      warn(e.message);
      return;
    }
    bad(e.message);
  } finally {
    busy = false;
    countChars();
  }
}

// ------------------------------------------------------------------
// 押す前にコメントを見せる(押すと確認済みになり、前の回へ移る)
// ------------------------------------------------------------------
// コメントは段階に結び付いている(kanban/domain/comments.py)。赤・黄・緑・赤を消す、の
// どのボタンでも、それまでのコメントは確認済みになる。**相手のコメントを読んでからでないと
// 押せない**: 未読のある看板は、押す前に中身を見せてから進める(もう読んだなら何も訊かない)。
// 届いたばかりで画面にまだ出ていない未読は、サーバが断って知らせる(send の unread_comments)。
const ACTION_LABEL = { size: '赤を付ける / 消す', ship: '発送(緑)', hold: '注文中(黄)' };

/** 見せて「進める」を選んだら true。相手からの未読が無ければ何も訊かずに true */
async function confirmComments(nos, action) {
  const targets = nos.map((no) => rowsByNo.get(String(no))).filter((r) => r && r.unread);
  if (!targets.length) return true;
  const threads = [];
  for (const r of targets) {
    try {
      const q = new URLSearchParams({ line, mgmt_no: r.mgmt_no, view: line });
      threads.push(await api.get(`/api/comments?${q}`));   // 開いた = 見せた(既読になる)
    } catch (e) { /* 取れなかった看板は飛ばす(押す操作は止めない) */ }
  }
  const shown = threads.filter((t) => t.open && t.open.length);
  if (!shown.length) return true;

  const modal = document.getElementById('cm-confirm');
  const list = document.getElementById('cm-confirm-list');
  document.getElementById('cm-confirm-action').textContent = action;
  list.innerHTML = '';
  for (const t of shown) {
    const head = document.createElement('b');
    head.className = 'cm-confirm__head';
    head.textContent = `${t.material} ${t.size}`;
    list.append(head);
    for (const c of t.open) list.append(c.kind === '確認' ? closeMark(c) : bubble(c, t.side));
  }
  modal.hidden = false;
  document.getElementById('cm-confirm-go').focus();
  const go = await new Promise((resolve) => {
    const done = (v) => {
      modal.hidden = true;
      document.getElementById('cm-confirm-go').onclick = null;
      document.getElementById('cm-confirm-stop').onclick = null;
      resolve(v);
    };
    document.getElementById('cm-confirm-go').onclick = () => done(true);
    document.getElementById('cm-confirm-stop').onclick = () => done(false);
  });
  // 見せたので未読の印を消した盤面にする(やめたときも、読んだことは変わらない)
  const last = threads[threads.length - 1];
  if (last && last.board) applyPayload({ board: last.board });
  return go;
}

// ------------------------------------------------------------------
function wireBoard() {
  const host = document.getElementById('board');
  if (!host) return;

  host.addEventListener('click', async (ev) => {
    const cell = ev.target.closest('.cell');
    if (!cell || cell.disabled || busy) return;
    const rowEl = cell.closest('.row');
    if (!rowEl) return;

    const kind = cell.dataset.kind;
    if (kind === 'comment') { openComments(line, rowEl.dataset.no); return; }

    // 赤なし・緑ありの行でサイズを押した → 出し直す前に確かめてもらう
    if (kind === 'size' && rowEl.classList.contains('row--alert')) {
      const a = alerts.find((x) => x.line === line && x.mgmt_no === rowEl.dataset.no);
      if (a && a.can_acknowledge) { await acknowledge(a); return; }
    }

    // **押すとコメントは確認済みになる**(前の回へ移る)。いまのコメントがあれば、押す前に見せる
    if (!(await confirmComments([rowEl.dataset.no], ACTION_LABEL[kind] || '押す'))) return;

    const body = {
      line,
      mgmt_no: rowEl.dataset.no,
      rev: Number(rowEl.dataset.rev),
    };

    // 「発注中 + 発送済み」でサイズを押したときだけ確認する。
    // VBA と同じ問いかけで、[はい] のときだけ両方を戻す
    if (kind === 'size' && cell.dataset.state === 'ordered') {
      const ship = rowEl.querySelector('.cell--ship');
      if (ship && ship.dataset.state === 'shipped') {
        if (!confirm('この資材は届いていますか？\n' +
                     '[OK]を選択すると、発注状態と発送状態の両方がクリアされます。')) return;
        body.delivered = true;
      }
    }

    await send(`/api/board/${kind === 'size' ? 'order' : kind}`, body, ACTION_LABEL[kind]);
  });
}

function wireToolbar() {
  on('refresh', async () => {
    const btn = document.getElementById('refresh');
    btn.disabled = true;
    btn.textContent = '↻ 読み込み中…';
    try {
      const r = await api.post(withLine('/api/refresh', line), {});
      applyPayload(r);
      if (r.imported === false) {
        warn(r.import_message || '共有DBから取り込めませんでした。前回取り込めた内容を表示しています。');
      } else {
        ok('更新しました');
      }
      loadReports();
    } catch (e) {
      bad(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '↻ 更新';
    }
  });

  on('batch-reset', async () => {
    const n = document.getElementById('c-reset').textContent;
    const nos = [...rowsByNo.values()].filter((r) => r.batch_reset).map((r) => r.mgmt_no);
    if (!(await confirmComments(nos, '届いた資材を一括確認する'))) return;
    if (!confirm(`${n} 件の資材到着を確認し、表示を元に戻しますか？`)) return;
    const r = await send('/api/board/batch-reset', { line }, '届いた資材を一括確認する');
    if (r) ok(`${r.done} 件をリセットしました`);
  });

  on('batch-ship', async () => {
    const n = document.getElementById('c-ship').textContent;
    const nos = [...rowsByNo.values()].filter((r) => r.batch_ship).map((r) => r.mgmt_no);
    if (!(await confirmComments(nos, 'まとめて発送済みにする'))) return;
    if (!confirm(`このラインの未発送品 ${n} 件を、まとめて発送済みにしますか？`)) return;
    const r = await send('/api/board/batch-ship', { line }, 'まとめて発送済みにする');
    if (r) ok(`${r.done} 件を発送済みにしました`);
  });
}

/** 送って、返ってきた盤面で描き直す。断られたときも描き直す */
async function send(path, body, action = '押す', tries = 0) {
  busy = true;
  try {
    const r = await api.post(path, body);
    applyPayload(r);
    return r;
  } catch (e) {
    // サーバは断るときも更新後の盤面を一緒に返す。押す前の状態に
    // 戻さず、**いま本当にどうなっているか**を出す
    applyPayload(e.body);
    if (e.code === 'unread_comments' && tries < 3) {
      // 届いたばかりの相手のコメントがあった(画面にまだ出ていなかった)。見せてから押し直す
      busy = false;
      if (await confirmComments((e.body && e.body.unread_nos) || [], action)) {
        return send(path, body, action, tries + 1);
      }
      return null;
    }
    if (e.code === 'cancelled_while_shipping') {
      // 画面が古くて、赤なし・緑ありに気付かずに押した。確かめてもらう
      const a = alerts.find((x) => x.line === body.line && x.mgmt_no === body.mgmt_no);
      if (a && a.can_acknowledge) { busy = false; await acknowledge(a); return null; }
      warn(e.message);
    } else if (e.status === 409) warn(e.message);
    else if (e.status === 422 || e.status === 403) warn(e.message);
    else bad(e.message);
    return null;
  } finally {
    busy = false;
  }
}

function on(id, fn) {
  const el = document.getElementById(id);
  if (el) el.addEventListener('click', fn);
}

// ------------------------------------------------------------------
// ライン切り替え
// ------------------------------------------------------------------
function wireTabs() {
  const tabs = document.getElementById('line-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', async (ev) => {
    const tab = ev.target.closest('.tab');
    if (!tab || busy) return;
    line = tab.dataset.line;
    tabs.querySelectorAll('.tab').forEach((t) => {
      t.setAttribute('aria-selected', String(t === tab));
    });
    await reload();
    loadReports();
  });
}

async function reload() {
  try {
    applyPayload(await api.get(withLine('/api/board', line)));
  } catch (e) {
    bad(e.message);
  }
}

// ------------------------------------------------------------------
// 印刷
// ------------------------------------------------------------------
async function loadReports() {
  const host = document.getElementById('reports');
  if (!host) return;
  try {
    const r = await api.get(withLine('/api/report/available', line));
    host.innerHTML = '';
    for (const rep of r.reports) {
      const a = document.createElement('a');
      a.className = 'btn btn--sm';
      a.href = `${rep.url}&t=${encodeURIComponent(window.APP.token)}`;
      a.target = '_blank';
      a.rel = 'noopener';
      a.dataset.title = rep.label;   // デスクトップ版の帳票の窓の題(desktop.js)
      a.innerHTML = `🖨 ${rep.label} <span class="count">${rep.count}</span>`;
      if (!rep.count) {
        // 0 件でも押せるようにしておく(「無い」ことを確かめたい場面がある)
        a.title = '印刷する明細はありません';
        a.style.opacity = '.6';
      }
      host.appendChild(a);
    }
  } catch (e) {
    host.innerHTML = '';
  }
}

// ------------------------------------------------------------------
// 自動更新
// ------------------------------------------------------------------
// 他の端末(倉庫や別の現場)が動かした結果を、押さなくても反映する。
// tkinter 版の `refresh_interval_sec` と同じ役目。
//
// **VBA 版は画面をダブルクリックしないと更新されなかった。** 押す人と
// 押さない人が出て、押し忘れた端末だけ古い盤を見ていた ── ここは
// 誰も押さなくても追いつくようにする。
//
// **変わっていなければ描き直さない。** 描き直すとホバー中のツールチップが
// 消え、押しかけたボタンが作り直される。
function startPolling() {
  const ms = window.APP.boardPollMs || 5000;
  if (poll) clearInterval(poll);

  const tick = async () => {
    if (busy || document.hidden) return;
    try {
      const s = await api.get('/api/status');
      setPending(s.pending, s.failures);
      setOpenTerminals(s.presence);
      setFreshness(s.import_age_sec, s.import_stale, s.last_import_at);
      if (s.token !== lastToken) {
        if (lastToken) await reload();
        lastToken = s.token;
      }
    } catch (e) { /* 接続断は health.js が帯で知らせる */ }
  };

  // **1 回目は待たない。** 開いた直後の数秒だけ「誰も開いていない」に
  // 見えると、身構えるための表示として意味をなさない
  tick();
  poll = setInterval(tick, ms);

  // **画面に戻ってきたら、待たずに 1 回取り直す。**
  //
  // 隠れているあいだは上の tick が何もしない。そのうえブラウザは裏の
  // タブの setInterval を間引く(Chrome は 5 分隠れると 1 分に 1 回まで
  // 落とす)ので、戻ってきた直後にそのまま待たせると、**最大 1 分ほど
  // 古い盤を見せてしまう** ── 見ている人には「更新されない」に見える。
  const catchUp = () => { if (!document.hidden) tick(); };
  document.addEventListener('visibilitychange', catchUp);
  window.addEventListener('focus', catchUp);
  window.addEventListener('pageshow', catchUp);
  // PC のスリープ明け・凍結が解けたとき(app.js が気付いて知らせる)。
  // 見えたままスリープしたタブには visibilitychange が来ない
  window.addEventListener('app-resumed', catchUp);
}

// 取り込みが止まっていないか(帯の「最終取り込み」)
//
// **自動更新は、動かなくなったことが見えて初めて信用できる。** 共有
// フォルダが落ちていると、画面は元気に動いているのに中身だけ古いまま
// になる ── VBA 版の「更新できていないのに気付かない」と同じ形。
// 取り込めていない時間が延びたら、そう言う。
function setFreshness(ageSec, stale, at) {
  const slot = document.getElementById('rb-fresh-slot');
  const el = document.getElementById('rb-fresh');
  if (!slot || !el) return;

  if (!stale) { slot.hidden = true; return; }
  slot.hidden = false;
  const min = Math.floor((ageSec || 0) / 60);
  el.textContent = min >= 1 ? `${min} 分前` : 'まだ一度も';
  el.title =
    '共有DBから取り込めていません。共有フォルダに届いているか確認してください。'
    + (at ? `\n最後に取り込めたのは ${at} です。` : '')
    + '\n盤の内容はそのときのままです。';
}
