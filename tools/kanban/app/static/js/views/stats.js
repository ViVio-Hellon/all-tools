// 看板集計(設定画面の「看板集計」タブ)
//
// 看板履歴.sqlite3 から組み立てた集計を出す(kanban/presenters/stats.py)。
// **読むだけなのでパスワードは訊かない。**
//
// ・グラフ … ライン × 月の線グラフ。提出率(%)・出した回数・届くまでの平均日数を
//             切り替える(目盛りは 1 本だけ。2 つの量を 1 枚に重ねない)
// ・色     … ラインの並び(LINE_MASTER)で決まった番号の色。絞り込んでも
//             残ったラインの色は変わらない(tokens.css の --series-N)
// ・凡例   … 押すとそのラインを隠す/出す。色だけに頼らないよう、4 本以下の
//             ときは線の端にも名前を書く。数字は「表で見る」にも全部ある
//
// 中身は**タブを初めて開いたときに**読みにいく(開かない人に共有フォルダを
// 往復させない)。

import { api } from '../api.js';
import { bad, ok } from '../toast.js';

const SVG = 'http://www.w3.org/2000/svg';
const HEIGHT = 300;
const PAD = { top: 14, right: 16, bottom: 30, left: 46 };
const DIRECT_LABEL_MAX = 4;          // これ以下の本数なら線の端に名前を書く

let data = null;
let measure = 'rate';                // 'rate' | 'count' | 'lead'
const hiddenLines = new Set();       // 凡例で隠したライン(読み直しても覚えておく)
let loaded = false;

export function wireStats() {
  const panel = document.getElementById('panel-stats');
  if (!panel) return;

  document.getElementById('stats-filter').addEventListener('submit', (ev) => {
    ev.preventDefault();
    load();
  });
  document.getElementById('stats-line').addEventListener('change', () => load());
  // 「保存」: 書き出し先のフォルダへ保存する(ダウンロードはリンクのまま)
  for (const b of document.querySelectorAll('[data-csv-save]')) {
    b.addEventListener('click', () => saveCsv(b));
  }
  for (const b of document.querySelectorAll('[data-measure]')) {
    b.addEventListener('click', () => {
      measure = b.dataset.measure;
      for (const x of document.querySelectorAll('[data-measure]')) {
        x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
      }
      render();
    });
  }
  // 幅が変わったら描き直す(線の位置は幅で決まる)
  if (window.ResizeObserver) {
    let last = 0;
    new ResizeObserver((entries) => {
      const w = Math.round(entries[0].contentRect.width);
      if (w && w !== last) { last = w; if (data) drawChart(); }
    }).observe(document.getElementById('stats-chart'));
  }
  // 初めて開いたときに読む。開いたまま再読込した(タブを覚えていた)ときも
  document.addEventListener('settings:tab', (ev) => {
    if (ev.detail === 'stats' && !loaded) load();
  });
  if (!panel.hidden) load();
}

// ------------------------------------------------------------------
// 読む
// ------------------------------------------------------------------
function query() {
  const p = new URLSearchParams();
  const from = document.getElementById('stats-from').value;
  const to = document.getElementById('stats-to').value;
  if (from) p.set('from', from);
  if (to) p.set('to', to);
  p.set('line', document.getElementById('stats-line').value);
  return p;
}

async function load() {
  loaded = true;
  const run = document.getElementById('stats-run');
  run.disabled = true;
  try {
    data = await api.get(`/api/stats?${query()}`);
  } catch (e) {
    bad(e.message);
    return;
  } finally {
    run.disabled = false;
  }
  // 空で送った(初回)ときは、サーバが決めた既定の期間を入れ物へ戻す
  document.getElementById('stats-from').value = data.months[0];
  document.getElementById('stats-to').value = data.months[data.months.length - 1];
  render();
}

// ------------------------------------------------------------------
// 描く
// ------------------------------------------------------------------
function render() {
  if (!data) return;
  const why = document.getElementById('stats-why');
  why.hidden = !data.why;
  why.textContent = data.why || '';
  renderOverall();
  document.getElementById('stats-since').textContent = data.recorded_since
    ? `${data.recorded_since} から記録があります。期間の中で出した回数は合わせて ${data.cycle_count} 回です。`
    : '';

  const single = data.selected_lines.length === 1;
  const label = single ? labelOf(data.selected_lines[0]) : 'ライン別';
  const MEASURES = {
    rate: ['提出率', '提出率 = その月に 1 回以上出した看板の枚数 ÷ そのラインの看板の枚数。'],
    count: ['出した回数', 'その月に出した回数(同じ看板を 2 回出せば 2 回)。'],
    lead: ['届くまでの平均日数',
      'その月に出した回が、出した日から届いた日(赤を消した日)まで平均何日かかったか。まだ届いていない回は入れません。'],
  };
  const [name, note] = MEASURES[measure];
  document.getElementById('stats-chart-title').textContent = `${name}(月ごと・${label})`;
  document.getElementById('stats-chart-note').textContent = note;

  renderLegend();
  drawChart();
  renderRateTable();
  renderSummary();
  renderHolds();
  renderUsage();
  renderKanbans();
  renderCsvLinks();
}

const days = (v) => (v === null || v === undefined ? '—' : `${v} 日`);
// 平均は小数 1 桁にそろえる(5 日と 5.5 日が並ぶと桁が読みにくい)
const avgDays = (v) => (v === null || v === undefined ? '—' : `${Number(v).toFixed(1)} 日`);

/** 期間全体の、届くまでの日数(平均・最短・最長) */
function renderOverall() {
  const o = data.overall || {};
  document.getElementById('stats-overall').hidden = !o.delivered;
  document.getElementById('ov-avg').textContent = avgDays(o.avg_deliver_days);
  document.getElementById('ov-min').textContent = days(o.min_deliver_days);
  document.getElementById('ov-max').textContent = days(o.max_deliver_days);
  document.getElementById('ov-n').textContent = `${o.delivered || 0} 回`;

  const h = data.holds_overall || {};
  const w = data.warehouse || {};
  document.getElementById('stats-overall2').hidden = !(h.holds || w.seen_count);
  document.getElementById('ov-holds').textContent =
    `${h.holds || 0} 回${h.held_kanbans ? `(${h.held_kanbans} 枚)` : ''}${h.holds_open ? ` うち ${h.holds_open} 回は注文中のまま` : ''}`;
  document.getElementById('ov-hold-days').textContent = avgDays(h.avg_hold_days);
  document.getElementById('ov-seen').textContent = hours(w.avg_seen_hours);
  document.getElementById('ov-seen-ship').textContent = hours(w.avg_seen_to_ship_hours);
  for (const el of document.querySelectorAll('.mistake-min')) el.textContent = data.mistake_minutes;
}

/** 時間(1 日を超えたら日も添える) */
function hours(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return n >= 24 ? `${n.toFixed(1)} 時間(約 ${(n / 24).toFixed(1)} 日)` : `${n.toFixed(1)} 時間`;
}

function lineInfo(code) {
  return data.lines.find((l) => l.code === code) || { code, label: code, slot: 0, total: 0 };
}
function labelOf(code) { return lineInfo(code).label; }
function colorOf(code) { return `var(--series-${(lineInfo(code).slot % 8) + 1})`; }

function valueOf(row) {
  if (!row) return null;
  if (measure === 'rate') return row.rate;
  if (measure === 'lead') return row.avg_deliver_days;
  return row.count;
}
function fmt(v) {
  if (v === null || v === undefined) return '—';
  if (measure === 'rate') return `${v.toFixed(1)}%`;
  if (measure === 'lead') return `${v.toFixed(1)} 日`;
  return `${v} 回`;
}

function visibleLines() {
  return data.selected_lines.filter((c) => !hiddenLines.has(c));
}

function renderLegend() {
  const box = document.getElementById('stats-legend');
  box.textContent = '';
  // 1 本だけなら凡例は要らない(見出しがラインを名乗っている)
  if (data.selected_lines.length < 2) return;
  for (const code of data.selected_lines) {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('aria-pressed', hiddenLines.has(code) ? 'false' : 'true');
    b.title = hiddenLines.has(code) ? '押すとグラフに出します' : '押すとグラフから隠します';
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = colorOf(code);
    b.append(sw, labelOf(code));
    b.addEventListener('click', () => {
      if (hiddenLines.has(code)) hiddenLines.delete(code); else hiddenLines.add(code);
      renderLegend();
      drawChart();
    });
    box.append(b);
  }
}

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

/** 目盛りの上限と刻み(回数・日数用)。1・2・5 の倍数に丸める。
 *  回数も日数も整数なので、刻みは 1 より細かくしない */
function niceScale(max) {
  if (max <= 0) return { top: 5, step: 1 };
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = Math.max(1, [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw));
  return { top: Math.ceil(max / step) * step, step };
}

function monthLabel(month, i, months) {
  const [y, m] = month.split('-');
  // 年は最初と 1 月にだけ付ける(毎回付けると読みにくい)
  return (i === 0 || m === '01' || months.length === 1) ? `${y}/${Number(m)}月` : `${Number(m)}月`;
}

function drawChart() {
  const box = document.getElementById('stats-chart');
  box.textContent = '';
  const months = data.months;
  const width = Math.max(320, box.clientWidth || 640);
  const lines = visibleLines();
  const direct = lines.length > 0 && lines.length <= DIRECT_LABEL_MAX && data.selected_lines.length > 1;
  const pad = { ...PAD, right: direct ? 64 : PAD.right };
  const plotW = width - pad.left - pad.right;
  const plotH = HEIGHT - pad.top - pad.bottom;

  const svg = el('svg', {
    viewBox: `0 0 ${width} ${HEIGHT}`, height: HEIGHT, role: 'img',
    'aria-label': document.getElementById('stats-chart-title').textContent + '(数字は「表で見る」にあります)',
  }, box);

  // 目盛り(提出率は 0〜100% に固定。回数は大きさに合わせる)
  let top; let step;
  if (measure === 'rate') { top = 100; step = 25; } else {
    let max = 0;
    for (const code of lines) {
      for (const r of data.rates[code] || []) max = Math.max(max, valueOf(r) || 0);
    }
    ({ top, step } = niceScale(max));
  }
  const x = (i) => pad.left + (months.length === 1 ? plotW / 2 : (plotW * i) / (months.length - 1));
  const y = (v) => pad.top + plotH - (plotH * v) / top;

  const grid = el('g', { class: 'grid' }, svg);
  const axis = el('g', { class: 'axis' }, svg);
  for (let v = 0; v <= top + 1e-9; v += step) {
    el('line', { x1: pad.left, x2: pad.left + plotW, y1: y(v), y2: y(v), class: v === 0 ? 'base' : '' }, grid);
    const t = el('text', { x: pad.left - 6, y: y(v) + 4, 'text-anchor': 'end' }, axis);
    t.textContent = measure === 'rate' ? `${v}%` : measure === 'lead' ? `${v}日` : String(v);
  }
  // 月の名前。多すぎるときは間引く(重ならないように)
  const every = Math.max(1, Math.ceil(months.length / Math.max(1, Math.floor(plotW / 52))));
  months.forEach((m, i) => {
    if (i % every && i !== months.length - 1) return;
    const t = el('text', { x: x(i), y: HEIGHT - 8, 'text-anchor': 'middle' }, axis);
    t.textContent = monthLabel(m, i, months);
  });

  if (!lines.length) {
    const t = el('text', { x: pad.left + plotW / 2, y: pad.top + plotH / 2, 'text-anchor': 'middle', class: 'empty-msg' }, svg);
    t.textContent = data.selected_lines.length ? 'ラインがすべて隠れています(凡例を押すと出ます)' : '出せるラインがありません';
    return;
  }

  // 線。値の無い月(看板が 0 枚で率が出ない)は線を切る
  const ends = [];
  for (const code of lines) {
    const rows = data.rates[code] || [];
    const g = el('g', { class: 'series' }, svg);
    g.style.color = colorOf(code);
    let d = '';
    let pen = false;
    rows.forEach((r, i) => {
      const v = valueOf(r);
      if (v === null || v === undefined) { pen = false; return; }
      d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    el('path', { d, stroke: 'currentColor' }, g);
    rows.forEach((r, i) => {
      const v = valueOf(r);
      if (v === null || v === undefined) return;
      el('circle', { cx: x(i), cy: y(v), r: 4, fill: 'currentColor' }, g);
    });
    const last = [...rows].reverse().findIndex((r) => valueOf(r) !== null && valueOf(r) !== undefined);
    if (last >= 0) {
      const i = rows.length - 1 - last;
      ends.push({ code, x: x(i), y: y(valueOf(rows[i])) });
    }
  }

  // 線の端の名前。重なるときは縦にずらす(文字は系列の色で塗らない)
  if (direct) {
    ends.sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i += 1) {
      if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
    }
    for (const e of ends) {
      const t = el('text', { x: e.x + 8, y: e.y + 4, class: 'end-label' }, svg);
      t.textContent = labelOf(e.code);
    }
  }

  wireHover(svg, box, { months, lines, x, pad, plotW, plotH });
}

/** 縦の線と吹き出し。いちばん近い月の、見えているライン全部の値を出す */
function wireHover(svg, box, { months, lines, x, pad, plotW, plotH }) {
  const cross = el('line', { class: 'crosshair', y1: pad.top, y2: pad.top + plotH, visibility: 'hidden' }, svg);
  const tip = document.createElement('div');
  tip.className = 'stats-tip';
  tip.hidden = true;
  box.append(tip);
  const hit = el('rect', {
    x: pad.left - 12, y: pad.top, width: plotW + 24, height: plotH, fill: 'transparent',
  }, svg);

  const show = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const scale = rect.width ? svg.viewBox.baseVal.width / rect.width : 1;
    const px = (clientX - rect.left) * scale;
    let best = 0;
    months.forEach((_, i) => { if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i; });
    cross.setAttribute('x1', x(best));
    cross.setAttribute('x2', x(best));
    cross.setAttribute('visibility', 'visible');

    tip.textContent = '';
    const head = document.createElement('b');
    head.textContent = months[best].replace('-', '年') + '月';
    tip.append(head);
    const rows = lines.map((code) => ({ code, r: (data.rates[code] || [])[best] }))
      .sort((a, b) => (valueOf(b.r) ?? -1) - (valueOf(a.r) ?? -1));
    for (const { code, r } of rows) {
      const line = document.createElement('div');
      const sw = document.createElement('span');
      sw.className = 'swatch';
      sw.style.background = colorOf(code);
      const v = document.createElement('span');
      v.className = 'v';
      v.textContent = r ? fmt(valueOf(r)) : '—';
      line.append(sw, labelOf(code), v);
      if (r && measure === 'rate' && r.total) {
        const s = document.createElement('small');
        s.textContent = `${r.submitted}/${r.total}枚`;
        line.append(s);
      }
      if (r && measure === 'lead' && r.delivered) {
        const s = document.createElement('small');
        s.textContent = `${r.delivered}回の平均`;
        line.append(s);
      }
      tip.append(line);
    }
    tip.hidden = false;
    const left = x(best) / scale;
    const tw = tip.offsetWidth;
    tip.style.left = `${Math.min(Math.max(0, left + 12), rect.width - tw)}px`;
    if (left + 12 + tw > rect.width) tip.style.left = `${Math.max(0, left - tw - 12)}px`;
    tip.style.top = `${pad.top / scale}px`;
  };
  const hide = () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); };
  hit.addEventListener('pointermove', (ev) => show(ev.clientX));
  hit.addEventListener('pointerdown', (ev) => show(ev.clientX));
  hit.addEventListener('pointerleave', hide);
}

// ------------------------------------------------------------------
// 表
// ------------------------------------------------------------------
function cell(tr, text, cls = '', tag = 'td') {
  const c = document.createElement(tag);
  c.textContent = text;
  if (cls) c.className = cls;
  tr.append(c);
  return c;
}

/** グラフと同じ数字を表で(色が見分けにくい人・印刷する人のため) */
function renderRateTable() {
  const table = document.getElementById('stats-rate-table');
  table.textContent = '';
  const head = document.createElement('tr');
  cell(head, 'ライン', '', 'th');
  cell(head, '看板の枚数', '', 'th');
  data.months.forEach((m, i) => cell(head, monthLabel(m, i, data.months), '', 'th'));
  table.append(head);
  for (const code of data.selected_lines) {
    const tr = document.createElement('tr');
    cell(tr, labelOf(code));
    cell(tr, `${lineInfo(code).total} 枚`, 'num');
    for (const r of data.rates[code] || []) {
      const c = cell(tr, fmt(valueOf(r)), 'num');
      c.title = `出した看板 ${r.submitted}/${r.total} 枚・出した回数 ${r.count} 回・`
        + `届くまで平均 ${avgDays(r.avg_deliver_days)}(${r.delivered} 回)`;
    }
    table.append(tr);
  }
}

function renderSummary() {
  const table = document.getElementById('stats-summary');
  table.textContent = '';
  const head = document.createElement('tr');
  for (const h of ['ライン', '看板の枚数', '出した回数', '週平均', '月平均',
    '届くまで(平均)', '最短', '最長', '次に出すまで(平均)', '未着', '注文中',
    '倉庫に出るまで', '出てから発送まで']) cell(head, h, '', 'th');
  table.append(head);
  for (const s of data.summary) {
    const tr = document.createElement('tr');
    cell(tr, s.label);
    cell(tr, `${lineInfo(s.line).total} 枚`, 'num');
    cell(tr, `${s.count} 回`, 'num');
    cell(tr, `${s.per_week} 回`, 'num');
    cell(tr, `${s.per_month} 回`, 'num');
    cell(tr, avgDays(s.avg_deliver_days), 'num');
    cell(tr, days(s.min_deliver_days), 'num');
    cell(tr, days(s.max_deliver_days), 'num');
    cell(tr, avgDays(s.avg_next_days), 'num');
    cell(tr, `${s.open} 回`, 'num');
    cell(tr, `${s.holds} 回`, 'num');
    cell(tr, hours(s.avg_seen_hours), 'num');
    cell(tr, hours(s.avg_seen_to_ship_hours), 'num');
    table.append(tr);
  }
}

/** よく切れる資材(注文中にした回数の多い看板から。サーバが並べて返す) */
function renderHolds() {
  const table = document.getElementById('stats-holds');
  if (!table) return;
  table.textContent = '';
  const head = document.createElement('tr');
  for (const h of ['ライン', '管理番号', '資材', 'サイズ', '注文中の回数', '平均', '最長', '合計', 'いま'])
    cell(head, h, '', 'th');
  table.append(head);
  const rows = data.hold_kanbans || [];
  if (!rows.length) {
    const tr = document.createElement('tr');
    cell(tr, '期間の中に注文中にした看板はありません').colSpan = 9;
    table.append(tr);
    return;
  }
  for (const k of rows) {
    const tr = document.createElement('tr');
    cell(tr, k.label);
    cell(tr, k.mgmt_no, 'num');
    cell(tr, k.material);
    cell(tr, k.size);
    cell(tr, `${k.holds} 回`, 'num');
    cell(tr, avgDays(k.avg_hold_days), 'num');
    cell(tr, days(k.max_hold_days), 'num');
    cell(tr, days(k.total_hold_days), 'num');
    cell(tr, k.open_since ? `注文中(${k.open_since} から ${k.open_days} 日)` : '');
    table.append(tr);
  }
}

/** 使用量(月あたりの多い看板から) */
function renderUsage() {
  const table = document.getElementById('stats-usage');
  if (!table) return;
  table.textContent = '';
  const head = document.createElement('tr');
  for (const h of ['ライン', '管理番号', '資材', 'サイズ', '1 回の量', '出した回数', '合計', '月あたり'])
    cell(head, h, '', 'th');
  table.append(head);
  const rows = data.usage || [];
  if (!rows.length) {
    const tr = document.createElement('tr');
    cell(tr, '期間の中に出した看板がありません').colSpan = 8;
    table.append(tr);
    return;
  }
  const amount = (v, unit) => (v === null || v === undefined ? '—' : `${v} ${unit || ''}`.trim());
  for (const k of rows) {
    const tr = document.createElement('tr');
    cell(tr, k.label);
    cell(tr, k.mgmt_no, 'num');
    cell(tr, k.material);
    cell(tr, k.size);
    cell(tr, k.per_order === null ? '(読めない)' : amount(k.per_order, k.unit), 'num');
    cell(tr, `${k.count} 回`, 'num');
    cell(tr, amount(k.total, k.unit), 'num');
    cell(tr, amount(k.per_month, k.unit), 'num');
    table.append(tr);
  }
}

/** 看板ごとの、届くまでの日数(平均の長い順。サーバが並べて返す) */
function renderKanbans() {
  const table = document.getElementById('stats-kanbans');
  table.textContent = '';
  const head = document.createElement('tr');
  for (const h of ['ライン', '管理番号', '資材', 'サイズ', '出した回数', '届いた回数',
    '届くまで(平均)', '最短', '最長', '次に出すまで(平均)']) cell(head, h, '', 'th');
  table.append(head);
  if (!data.kanbans.length) {
    const tr = document.createElement('tr');
    cell(tr, '期間の中に出した看板がありません').colSpan = 10;
    table.append(tr);
    return;
  }
  for (const k of data.kanbans) {
    const tr = document.createElement('tr');
    cell(tr, k.label);
    cell(tr, k.mgmt_no, 'num');
    cell(tr, k.material);
    cell(tr, k.size);
    cell(tr, `${k.count} 回`, 'num');
    cell(tr, `${k.delivered} 回`, 'num');
    cell(tr, avgDays(k.avg_deliver_days), 'num');
    cell(tr, days(k.min_deliver_days), 'num');
    cell(tr, days(k.max_deliver_days), 'num');
    cell(tr, avgDays(k.avg_next_days), 'num');
    table.append(tr);
  }
}

// ------------------------------------------------------------------
// CSV
// ------------------------------------------------------------------
// 画面の期間・ラインのまま書き出す。リンクなので起動トークンは ?t= で渡す
// (ヘッダを付けられない)。読めないときは押せなくする(断りの JSON を
// ファイルとして保存させない)。
/** いまの期間・ラインで、CSV を書き出し先のフォルダへ保存する。結果(保存した場所)は消えない所に残す */
async function saveCsv(button) {
  if (!data) return;
  const box = document.getElementById('csv-result');
  button.disabled = true;
  try {
    const r = await api.post('/api/stats/csv/save', {
      kind: button.dataset.csvSave,
      from: data.months[0], to: data.months[data.months.length - 1],
      line: document.getElementById('stats-line').value,
    });
    ok('CSV を保存しました');
    box.className = 'banner banner--ok';
    box.textContent = `✔ 保存しました: ${r.path}`;
    box.hidden = false;
  } catch (e) {
    bad(e.message);
    box.className = 'banner banner--bad';
    box.textContent = e.message;
    box.hidden = false;
  } finally {
    button.disabled = !data.available;
  }
}

function renderCsvLinks() {
  const dest = document.getElementById('csv-dest');
  if (dest && data.csv_dir) dest.textContent = data.csv_dir;
  for (const b of document.querySelectorAll('[data-csv-save]')) b.disabled = !data.available;
  const p = new URLSearchParams({
    from: data.months[0], to: data.months[data.months.length - 1],
    line: document.getElementById('stats-line').value,
  });
  for (const a of document.querySelectorAll('[data-csv]')) {
    const q = new URLSearchParams(p);
    q.set('kind', a.dataset.csv);
    q.set('t', window.APP.token);
    a.href = `/api/stats/csv?${q}`;
    a.setAttribute('aria-disabled', data.available ? 'false' : 'true');
  }
}
