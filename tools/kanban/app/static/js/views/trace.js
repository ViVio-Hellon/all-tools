// 記録(設定画面の「記録」タブ) ── 後追い・なぜなぜ分析(kanban/trace.py)
//
// ・探す   … 期間・端末・種類・文字で絞る。新しい順
// ・開く   … 1 件の 何が・いつ・どこで・誰が・入力・原因の連鎖・直前の操作・その時の状態
// ・シート … なぜなぜ分析シートの下書き(画面で直してテキストで保存)
//
// **読むだけなのでパスワードは訊かない。** 中身は「記録」タブを初めて開いたときに読む。

import { api } from '../api.js';
import { bad, ok } from '../toast.js';

let loaded = false;
let current = null;   // 開いている 1 件

export function wireTrace() {
  const panel = document.getElementById('panel-trace');
  if (!panel) return;
  const to = new Date();
  const from = new Date(to.getTime() - 6 * 86400000);
  document.getElementById('trace-from').value = iso(from);
  document.getElementById('trace-to').value = iso(to);

  document.getElementById('trace-filter').addEventListener('submit', (ev) => { ev.preventDefault(); search(); });
  for (const box of document.querySelectorAll('#trace-kinds input')) box.addEventListener('change', search);
  document.getElementById('trace-pc').addEventListener('change', search);
  document.getElementById('trace-open-ref').addEventListener('click', () => {
    const ref = document.getElementById('trace-ref').value.trim();
    if (ref) openRecord(ref);
  });
  document.getElementById('trace-ref').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); document.getElementById('trace-open-ref').click(); }
  });
  document.getElementById('trace-sheet-save').addEventListener('click', saveSheet);
  document.getElementById('trace-sheet-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(document.getElementById('trace-sheet').value);
      ok('コピーしました');
    } catch (e) {
      document.getElementById('trace-sheet').select();
      bad('コピーできませんでした。選んだ状態にしたので Ctrl+C でコピーしてください');
    }
  });

  document.addEventListener('settings:tab', (ev) => {
    if (ev.detail === 'trace' && !loaded) search();
  });
  if (!panel.hidden) search();
}

function iso(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function params() {
  const kinds = [...document.querySelectorAll('#trace-kinds input:checked')].map((x) => x.value);
  return new URLSearchParams({
    from: document.getElementById('trace-from').value,
    to: document.getElementById('trace-to').value,
    pc: document.getElementById('trace-pc').value,
    q: document.getElementById('trace-q').value.trim(),
    kinds: kinds.join(','),
  });
}

async function search() {
  loaded = true;
  const p = params();
  const csv = new URLSearchParams(p);
  csv.set('t', window.APP.token);
  document.getElementById('trace-csv').href = `/api/trace/csv?${csv}`;
  const summary = document.getElementById('trace-summary');
  summary.textContent = '探しています…';
  try {
    const r = await api.get(`/api/trace?${p}`);
    fillPcs(r);
    summary.textContent = !r.exists
      ? `まだ記録がありません(置き場所: ${r.root})`
      : `${r.from} 〜 ${r.to}: ${r.count} 件${r.count >= r.limit ? `(新しいほうから ${r.limit} 件まで)` : ''}  置き場所: ${r.root}`;
    const body = document.getElementById('trace-rows');
    body.innerHTML = '';
    for (const x of r.records) {
      const tr = document.createElement('tr');
      tr.className = `trace-row trace-row--${x.kind}`;
      tr.tabIndex = 0;
      tr.title = 'クリックで開く';
      // 一覧は短く(全文は開いたときに)
      const cut = (t, n) => (t && t.length > n ? `${t.slice(0, n)}…` : t || '');
      const cells = [x.at.slice(5, 19), x.kind_label, x.pc, cut(x.path || x.where, 60), cut(x.shown || x.what, 140), x.ref];
      for (const [i, text] of cells.entries()) {
        const td = document.createElement('td');
        td.textContent = text;
        if (i === 4) td.className = 'trace-what';
        tr.appendChild(td);
      }
      tr.addEventListener('click', () => openRecord(x.ref));
      tr.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') openRecord(x.ref); });
      body.appendChild(tr);
    }
    if (!r.records.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 6;
      td.textContent = '条件に合う記録はありません。';
      tr.appendChild(td);
      body.appendChild(tr);
    }
  } catch (e) {
    summary.textContent = `読めませんでした: ${e.message}`;
  }
}

/** 端末の選択肢(この端末 / すべて / 記録のある PC) */
function fillPcs(r) {
  const select = document.getElementById('trace-pc');
  const keep = select.value;
  const have = new Set([...select.options].map((o) => o.value));
  for (const pc of r.pcs || []) {
    if (have.has(pc)) continue;
    const o = document.createElement('option');
    o.value = pc;
    o.textContent = pc + (pc === r.me ? '(この端末)' : '');
    select.appendChild(o);
  }
  select.value = keep;
}

async function openRecord(ref) {
  try {
    const r = await api.get(`/api/trace/item?ref=${encodeURIComponent(ref)}`);
    current = r;
    render(r);
    document.getElementById('trace-detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    bad(e.message);
  }
}

function render(r) {
  const x = r.record;
  const req = x.request || {};
  const res = x.response || {};
  document.getElementById('trace-detail').hidden = false;
  document.getElementById('trace-detail-title').textContent = `${r.kind_label}  ${x.ref}`;
  kv(document.getElementById('trace-facts'), [
    ['何が', x.what],
    ['画面に出した文言', res.message ? `${res.message}(理由コード ${res.code || '-'} / ${res.status})` : ''],
    ['いつ', x.at],
    ['どの端末', `${x.pc}(ログインID ${x.login || '-'})`],
    ['モード・ライン', `${x.mode || '-'} / ${x.line || '-'}`],
    ['どこで', [x.where, req.path ? `${req.method} ${req.path}` : ''].filter(Boolean).join(' / ')],
    ['そのときの入力', req.body || req.args ? JSON.stringify(req.body || req.args) : ''],
    ['同じ操作の記録', (r.related || []).map((t) => `${t.kind_label}: ${t.what}(${t.where})`).join(' / ')],
    ['版', x.version],
    ['記録のファイル', r.file],
  ]);
  const chain = document.getElementById('trace-chain');
  chain.innerHTML = '';
  const links = (x.exception && x.exception.chain) || [];
  for (const c of links) {
    const li = document.createElement('li');
    li.textContent = `${c.type}: ${c.message}${c.at ? `  (${c.at})` : ''}`;
    chain.appendChild(li);
  }
  if (!links.length) chain.innerHTML = '<li class="muted">例外はありません(断った理由・警告の文が事象です)</li>';

  const trail = document.getElementById('trace-trail');
  trail.innerHTML = '';
  for (const t of r.trail) {
    const li = document.createElement('li');
    const what = t.shown || t.what || '';
    li.textContent = `${t.at.slice(11, 19)} ${t.kind_label} ${t.path || t.where} ${what.length > 120 ? `${what.slice(0, 120)}…` : what}`;
    li.title = t.ref;
    trail.appendChild(li);
  }
  if (!r.trail.length) trail.innerHTML = '<li class="muted">記録なし</li>';

  kv(document.getElementById('trace-state'), Object.entries(x.state || {}));
  document.getElementById('trace-raw').textContent = JSON.stringify(
    { request: x.request, response: x.response, extra: x.extra,
      traceback: x.exception && x.exception.traceback }, null, 2);
  document.getElementById('trace-sheet').value = r.sheet;
}

function kv(dl, pairs) {
  dl.innerHTML = '';
  for (const [k, v] of pairs) {
    if (v === undefined || v === null || v === '') continue;
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = typeof v === 'object' ? JSON.stringify(v) : String(v);
    dl.append(dt, dd);
  }
  if (!dl.children.length) dl.innerHTML = '<dd class="muted">記録なし</dd>';
}

/** なぜなぜ分析シートをテキストで保存(ブラウザのダウンロード先へ) */
function saveSheet() {
  if (!current) return;
  const text = document.getElementById('trace-sheet').value;
  const blob = new Blob(['﻿' + text.replace(/\n/g, '\r\n')], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `なぜなぜ_${current.record.ref}.txt`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
