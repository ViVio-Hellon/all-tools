// 設定画面
//
// モードの切り替えは**アクセス権限**(梱包資材マスタ。ログインID・PC名)で決まり、
// パスワードは訊かない。接続先などの変更は管理者パスワードで守る。守られた操作の直前に訊くが、
// **認証済みなら訊かない**(「パスワード認証」で開ける。確認画面で入れても
// 開く)。認証は使わない時間が続くと切れる(kanban/admin_lock.py)。

import { api } from '../api.js';
import * as desktop from '../desktop.js';
import { bad, ok, warn } from '../toast.js';
import { wireStats } from './stats.js';
import { wireTrace } from './trace.js';

let state = null;

export function start(initial) {
  state = initial;
  // デスクトップ版の「名前を付けて保存」は CSV の出力先から始める
  desktop.setSaveDir(state.csv_dir || '');

  const mode = document.getElementById('mode');
  if (mode) mode.addEventListener('change', onModeChange);

  const line = document.getElementById('line');
  if (line) line.addEventListener('change', onLineChange);

  const save = document.getElementById('save-accdb');
  if (save) save.addEventListener('click', onAccdbChange);
  wireSource();

  wireStats();       // タブより先に: 覚えていた面が看板集計なら、開いた知らせを受け取る
  wireTrace();       // 同じく「記録」
  wireTabs();
  wireAccess();
  wireHistoryPath();
  wireCsvDir();
  wireLogDir();
  wireMistake();
  wireBrowser();
  wireRefresh();
  wireDistribution();
  wireBehavior();
  wireUnlock();
  wirePassword();
  on('reimport', onReimport);
  on('retry-failed', () => onFailed('/api/sync/retry-failed',
    'あきらめた操作を、もう一度共有DBへ送ります。', null));
  on('discard-failed', () => onFailed('/api/sync/discard-failed',
    '送れない操作を捨てて、共有DBの今の状態に戻します。',
    'この端末で押したが共有DBへ届かなかった操作を捨てます。\n'
    + '共有DBにある看板は共有DBの状態に、共有DBに無い看板は手元からも消えます。\n\n'
    + '捨てた操作は戻せません(ログには残ります)。よろしいですか？'));
  on('master-open', () => openTable(0));
  on('master-add', showAddForm);
  loadMaster();
}

/** 要確認の片付け(捨てる / もう一度送る)。終わったら数字を出し直すため読み直す */
async function onFailed(path, note, question) {
  if (question && !confirm(question)) return;
  const creds = await askPassword(note);
  if (!creds) return;
  try {
    const r = await api.post(path, { ...creds });
    ok(r.message);
    setTimeout(() => location.reload(), 1500);
  } catch (e) {
    bad(e.message);
  }
}

/** id で拾って click を繋ぐ小道具(無ければ何もしない)。 */
function on(id, fn) {
  const el = document.getElementById(id);
  if (el) el.addEventListener('click', fn);
}

// ------------------------------------------------------------------
// タブ
// ------------------------------------------------------------------
// 8 つの用が1ページに集まっているので、見る面だけを切り替える。
//
// **中身は作り直さない。** 隠して出すだけにしてあるので、マスタ管理の表を
// 開いたまま別の面へ行って戻っても、読み直しは起きない(共有フォルダへの
// 往復を増やさない)。
//
// 開いた面は端末に覚えさせる ── 同じ用で来る人が多いため。覚えられない
// ブラウザ(プライベート窓など)でも、既定の面が出るだけで困らない。
const TAB_MEMORY = 'kanban.settings.tab';

// ------------------------------------------------------------------
// 配布設定(kanban/distribution.py。python-web-tools と同じつくり)
// ------------------------------------------------------------------
// 1 台で決めた設定を「配布設定」フォルダへ書き出し、フォルダごと配った先が
// 起動時に読み込む。**書き出した直後に、配った先と同じ読み方で読み戻して
// 確かめた結果を出す**(フォルダができたか・中身が揃ったかを探しに行かせない)。
function wireDistribution() {
  const send = async (path, body, note) => {
    const creds = await askPassword(note);
    if (!creds) return null;
    try {
      const r = await api.post(path, { ...body, ...creds });
      ok(r.message);
      if (r.distribution) renderDistribution(r.distribution);
      renderDistChecks(r.checks || []);
      return r;
    } catch (e) {
      bad(e.message);
      if (e.body && e.body.checks) renderDistChecks(e.body.checks);
      if (e.body && e.body.distribution) renderDistribution(e.body.distribution);
      return null;
    }
  };
  const items = () => [...document.querySelectorAll('[data-dist-item]')]
    .filter((c) => c.checked).map((c) => c.dataset.distItem);

  on('dist-export', () => {
    const chosen = items();
    if (!chosen.length) { bad('入れる項目を 1 つ以上選んでください'); return; }
    send('/api/distribution/export', { items: chosen },
      'この端末の設定を配布設定に書き出します(前の中身は置き換えます)。');
  });
  on('dist-reapply', () => send('/api/distribution/reapply', {},
    '配布設定を読み込み直します。この端末で変えてある設定も上書きします。')
    .then((r) => { if (r) setTimeout(() => location.reload(), 1500); }));
  on('dist-remove', () => send('/api/distribution/remove', {},
    '配布設定フォルダを消します。この端末の設定はそのままです。'));
  on('dist-build', async () => {
    const btn = document.getElementById('dist-build');
    const creds = await askPassword('配布用フォルダを、アプリのフォルダの隣に作ります。');
    if (!creds) return;
    btn.disabled = true;
    const out = document.getElementById('dist-build-result');
    try {
      const r = await api.post('/api/distribution/build', { ...creds });
      ok(r.message);
      out.textContent = (r.lines || []).join('\n');
      out.hidden = false;
    } catch (e) {
      bad(e.message);
      out.textContent = e.message;
      out.hidden = false;
    } finally {
      btn.disabled = false;
    }
  });
}

/** いま置いてある配布設定を描き直す。**文字は textContent で入れる** */
function renderDistribution(d) {
  const stateEl = document.getElementById('dist-state');
  if (!stateEl) return;
  stateEl.textContent = d.exists ? 'あり' : 'なし';
  stateEl.classList.toggle('is-ok', d.exists);
  document.getElementById('dist-meta').textContent = d.exists
    ? `${d.created_at} に ${d.created_on} で作成`
    : 'まだありません。下の「配布設定を書き出す」で作られます。';
  const table = document.getElementById('dist-contents');
  table.querySelectorAll('tr:not(:first-child)').forEach((tr) => tr.remove());
  for (const c of d.contents || []) {
    const tr = document.createElement('tr');
    for (const text of [c.label, c.value]) {
      const td = document.createElement('td');
      td.textContent = text;
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }
  document.getElementById('dist-contents-wrap').hidden = !d.exists;
  document.getElementById('dist-reapply').disabled = !d.exists;
  document.getElementById('dist-remove').disabled = !d.exists;
}

function renderDistChecks(checks) {
  const box = document.getElementById('dist-checks');
  const list = document.getElementById('dist-checks-list');
  if (!box || !list) return;
  list.innerHTML = '';
  for (const c of checks) {
    const li = document.createElement('li');
    li.className = c.ok ? 'is-ok' : 'is-ng';
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = c.ok ? '✓' : '✗';
    li.appendChild(mark);
    li.appendChild(document.createTextNode(c.label));
    if (c.detail) {
      const small = document.createElement('small');
      small.textContent = ` ${c.detail}`;
      li.appendChild(small);
    }
    list.appendChild(li);
  }
  box.hidden = !checks.length;
  box.classList.toggle('is-ng', checks.some((c) => !c.ok));
}

// ------------------------------------------------------------------
// 動作(取り込み・書き戻し間隔、自動印刷)
// ------------------------------------------------------------------
function wireBehavior() {
  const form = document.getElementById('behavior-form');
  if (!form) return;
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const values = {
      import_interval_sec: form.elements.import_interval_sec.value.trim(),
      export_interval_sec: form.elements.export_interval_sec.value.trim(),
      auto_print: form.elements.auto_print.checked,
    };
    const creds = await askPassword('動作の設定を保存します(この端末の設定。次にアプリを開いたときから効きます)。');
    if (!creds) return;
    const btn = document.getElementById('behavior-save');
    btn.disabled = true;
    try {
      const r = await api.post('/api/behavior', { values, ...creds });
      ok(r.message);
      setTimeout(() => location.reload(), 1200);
    } catch (e) {
      bad(e.message);
      const target = e.field && form.elements[e.field];
      if (target) target.focus();
      btn.disabled = false;
    }
  });
}

// ------------------------------------------------------------------
// パスワード認証
// ------------------------------------------------------------------
// **認証するためだけの入口。** 以前は守られた操作を押したときに出る確認
// 画面でしか入れられず、マスタを 5 マス直せば 5 回打つことになっていた。
//
// 状態は 3 つ(未設定 / 未認証 / 認証済み)。**どれも画面の中で切り替える**
// ── 別の面の操作でパスワードを決めた・認証した、のあとで読み直さずに戻って
// きても、古い表示(「未設定です」など)を出したままにしない。
function wireUnlock() {
  const setup = document.getElementById('setup-form');
  if (setup) {
    setup.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const pw = setup.elements.new_password;
      const confirm = setup.elements.new_password_confirm;
      if (!pw.value) { bad('パスワードを入れてください'); pw.focus(); return; }
      if (pw.value !== confirm.value) { bad('確認用のパスワードが一致しません'); confirm.focus(); return; }
      try {
        const r = await api.post('/api/admin/password',
          { new_password: pw.value, new_password_confirm: confirm.value });
        setup.reset();
        ok(r.message);
        refreshLock();
      } catch (e) {
        bad(e.message);
      }
    });
  }

  const form = document.getElementById('unlock-form');
  if (form) {
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const input = form.elements.password;
      if (!input.value) { bad('パスワードを入れてください'); input.focus(); return; }
      try {
        const r = await api.post('/api/admin/unlock', { password: input.value });
        input.value = '';
        ok(r.message);
        applyLock(r);
      } catch (e) {
        bad(e.message);
        input.value = '';
        input.focus();
      }
    });
  }
  const lockNow = async () => {
    try {
      const r = await api.post('/api/admin/lock', {});
      ok(r.message);
      applyLock(r);
    } catch (e) {
      bad(e.message);
    }
  };
  on('unlock-lock', lockNow);
  on('lockbar-lock', lockNow);
  on('lockbar-go', () => {
    const tab = document.getElementById('tab-auth');
    if (tab) tab.click();
  });
  // 放っておくと解除される。帯が「認証済み」のまま残らないよう、ときどき
  // 確かめる(手元のサーバへの問い合わせなので軽い)。タブに戻ったときも
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshLock(); });
  setInterval(refreshLock, 60 * 1000);
}

async function refreshLock() {
  try {
    applyLock(await api.get('/api/admin/state'));
  } catch (e) { /* 接続断は health.js が帯で知らせる */ }
}

/** 認証の状態を画面(タブの下の帯・パスワード認証の面)へ反映する */
function applyLock(st) {
  if (!st || !('admin_unlocked' in st)) return;
  if ('has_admin_password' in st) state.has_admin_password = st.has_admin_password;
  state.admin_unlocked = st.admin_unlocked;
  const has = !!state.has_admin_password;
  const open = has && st.admin_unlocked;
  const show = (id, visible) => {
    const el = document.getElementById(id);
    if (el) el.hidden = !visible;
  };

  const bar = document.getElementById('lockbar');
  if (bar) {
    bar.classList.toggle('is-open', open);
    bar.classList.toggle('is-unset', !has);
    document.getElementById('lockbar-text').textContent = !has
      ? '管理者パスワードが未設定です ── 「パスワード認証」で決めてください'
      : open ? '認証済み ── 設定を変えるときにパスワードを訊きません'
        : '未認証 ── 設定を変えるときにパスワードを訊きます';
    show('lockbar-lock', open);
    show('lockbar-go', !open);
  }

  const chip = document.getElementById('auth-state');
  if (chip) {
    chip.textContent = !has ? '未設定' : open ? '認証済み' : '未認証';
    chip.classList.toggle('is-ok', open);
  }
  show('setup-form', !has);
  show('unlock-form', has && !open);
  show('auth-ok', open);
  show('password-card', has);
  const status = document.getElementById('unlock-status');
  if (status && open) {
    const min = Math.floor((st.admin_unlock_remaining_sec || 0) / 60) + 1;
    status.textContent = '認証済みです。設定を変えるときにパスワードを訊きません'
      + `(あと ${min} 分ほど。操作するたびに延びます)。`;
  }
}

// ------------------------------------------------------------------
// 管理者パスワード
// ------------------------------------------------------------------
// **パスワードだけを入れる場所。** 以前は操作のときに出る確認画面しか
// 無く、1 度決めたら変える手段が無かった。確認画面(askPassword)は
// そのまま残す ── 必要なところで訊くのは変えない。
function wirePassword() {
  const form = document.getElementById('password-form');
  if (!form) return;

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const field = (name) => form.elements[name];
    const body = {
      current_password: field('current_password') ? field('current_password').value : '',
      new_password: field('new_password').value,
      new_password_confirm: field('new_password_confirm').value,
    };
    // 送る前に分かることは、送る前に言う
    if (!body.new_password) {
      bad('新しいパスワードを入れてください');
      field('new_password').focus();
      return;
    }
    if (body.new_password !== body.new_password_confirm) {
      bad('確認用のパスワードが一致しません');
      field('new_password_confirm').focus();
      return;
    }

    const btn = document.getElementById('password-save');
    btn.disabled = true;
    try {
      const r = await api.post('/api/admin/password', body);
      ok(r.message);
      form.reset();
      btn.disabled = false;
      refreshLock();
    } catch (e) {
      bad(e.message);
      const target = e.field && form.elements[e.field];
      if (target) { target.value = ''; target.focus(); }
      btn.disabled = false;
    }
  });
}

function wireTabs() {
  const tabs = [...document.querySelectorAll('.tabs--settings .tab')];
  if (!tabs.length) return;

  const show = (key) => {
    let found = false;
    for (const tab of tabs) {
      const on = tab.dataset.tab === key;
      found = found || on;
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      const panel = document.getElementById(`panel-${tab.dataset.tab}`);
      if (panel) panel.hidden = !on;
    }
    if (!found) return false;
    try { localStorage.setItem(TAB_MEMORY, key); } catch (e) { /* 覚えられなくてよい */ }
    // 開いたときに初めて読みにいく面(看板集計)へ知らせる
    document.dispatchEvent(new CustomEvent('settings:tab', { detail: key }));
    return true;
  };

  for (const tab of tabs) {
    tab.addEventListener('click', () => show(tab.dataset.tab));
  }
  // ← → で隣の面へ(キーボードだけでも回れるように)
  for (const [i, tab] of tabs.entries()) {
    tab.addEventListener('keydown', (ev) => {
      const step = ev.key === 'ArrowRight' ? 1 : ev.key === 'ArrowLeft' ? -1 : 0;
      if (!step) return;
      ev.preventDefault();
      const next = tabs[(i + step + tabs.length) % tabs.length];
      show(next.dataset.tab);
      next.focus();
    });
  }

  let saved = null;
  try { saved = localStorage.getItem(TAB_MEMORY); } catch (e) { saved = null; }
  if (!saved || !show(saved)) show(tabs[0].dataset.tab);
}

// ------------------------------------------------------------------
// モード
// ------------------------------------------------------------------
// **関門はアクセス権限**(梱包資材マスタの「アクセス権限」をログインID・PC名で引く)。
// パスワードは訊かない ── パスワードは教え合えるが、IDとPC名は画面から変えられない。
// 権限の無いモードは選べないようにしてあるが、**サーバも毎回確かめる**(行を消した直後など)
async function onModeChange(ev) {
  const next = ev.target.value;
  if (next === state.mode) return;

  try {
    const r = await api.post('/api/mode', { mode: next });
    if (!r.changed) { ok('変更はありません'); return; }
    // **デスクトップ版はその場で切り替えられる。** ポートを使わないので、外枠が
    // Python だけを立て直す(書き戻しを済ませてから)。窓はそのまま
    if (desktop.isDesktop) {
      if (confirm(`${r.label}に切り替えます。\n共有DBへの書き戻しを済ませてから、${r.label}で開き直します。\n\nいま切り替えますか？`
        + '\n(「キャンセル」なら、次にアプリを開いたときから切り替わります)')) {
        showModeNotice(r.label, '', true);
        await desktop.restartApp();
        return;
      }
      ok(`次にアプリを開いたときから${r.label}になります。`);
      showModeNotice(r.label);
      return;
    }
    // **いま動いているプロセスのモードは変わらない。**
    // モードはポート(現場 8741 / 倉庫 8751 / 倉庫参照 8761)とロックに
    // 結びついているので、走ったまま入れ替えられない。開き直すと、
    // 起動側が古いモードを終わらせて新しいモードで立て直す
    ok(r.message || `次にアプリを開いたときから${r.label}になります。`);
    showModeNotice(r.label);
  } catch (e) {
    ev.target.value = state.mode;
    if (e.code === 'no_permission') {
      // 何を足せば使えるのかまで、消えない場所に出す
      showModeNotice('', e.message);
      if (e.body && e.body.access) renderAccess(e.body.access);
    }
    bad(e.message);
  }
}

/** 「開き直すと変わる」(または、権限が無くて変えられなかった理由)を画面に残す。トーストは消えてしまう。 */
function showModeNotice(label, refused, switching = false) {
  const box = document.getElementById('mode-notice');
  if (!box) return;
  box.className = `banner ${refused ? 'banner--bad' : 'banner--warn'}`;
  box.style.whiteSpace = 'pre-line';
  box.textContent = refused || (switching
    ? `${label}に切り替えています…(書き戻しを済ませてから開き直します)`
    : `次にアプリを開いたときから${label}になります。`
      + 'いまの画面は開いたときのモードのままです'
      + (desktop.isDesktop ? '。'
        : '(モードはポートに結びついているため、走ったままでは切り替わりません)。'));
  box.hidden = false;
}

// ------------------------------------------------------------------
// この端末の権限(kanban/access_control.py)
// ------------------------------------------------------------------
// 起動したときに読んだ権限をまず出し、すぐに読み直す(マスタ管理で足した行を
// 開き直さずに確かめられるように)。
function wireAccess() {
  // 担当ラインを変えた直後(読み直したあと)に「次に起動すると戻る」を出し直す
  try {
    const note = sessionStorage.getItem('kanban.settings.lineNote');
    if (note) {
      sessionStorage.removeItem('kanban.settings.lineNote');
      showModeNotice('', note);
    }
  } catch (e) { /* 無くてよい */ }
  if (!document.getElementById('access-card')) return;
  renderAccess(state.access);
  refreshAccess();
  on('access-reload', refreshAccess);
  on('access-edit', openAccessTable);
  on('access-save', onAccessPathChange);
}

async function refreshAccess() {
  const src = document.getElementById('access-source');
  if (src) src.textContent = '読んでいます…';
  try {
    const a = await api.get('/api/access');
    renderAccess(a);
    renderAccessPath(a);
  } catch (e) {
    if (src) src.textContent = `読めませんでした: ${e.message}`;
  }
}

/** 権限の中身を描く。モードの選択肢も付け直す(権限の無いモードは選べない) */
function renderAccess(a) {
  if (!a) return;
  state.access = { ...(state.access || {}), ...a };
  const g = a.grant;
  if (!g) return;
  document.getElementById('access-login').textContent = g.login_id || '(不明)';
  document.getElementById('access-pc').textContent = g.pc_name || '(不明)';

  const list = document.getElementById('access-modes');
  list.innerHTML = '';
  for (const m of g.modes) {
    const li = document.createElement('li');
    li.className = m.allowed ? 'is-ok' : 'is-no';
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = m.allowed ? '✔' : '✖';
    const name = document.createElement('b');
    name.textContent = m.label;
    const how = document.createElement('span');
    how.className = 'how';
    how.textContent = m.allowed
      ? (m.permission ? `使えます(${m.via || m.permission})` : '使えます(権限は問いません)')
      : `権限なし ── ${m.how}`;
    li.append(mark, name, how);
    list.appendChild(li);
  }

  // 担当ライン(表から)と、読まなかった権限(正しい書き方を添えて)
  const lineEl = document.getElementById('access-line');
  if (lineEl) {
    lineEl.textContent = g.line_label || (g.line_problem ? '決めません' : 'ラインの行なし');
    const note = document.getElementById('access-line-note');
    const noteText = [a.line_applied, g.line_problem].filter(Boolean).join('\n');
    note.className = `banner ${a.line_applied ? 'banner--ok' : 'banner--warn'}`;
    note.style.whiteSpace = 'pre-line';
    note.textContent = noteText;
    note.hidden = !noteText;
    const ign = document.getElementById('access-ignored');
    ign.textContent = (g.ignored || []).length ? `読まなかった権限: ${g.ignored.join('、')}` : '';
    ign.hidden = !(g.ignored || []).length;
    if (a.line_applied) {
      ok(a.line_applied);
      state.line = a.current_line;
      const sel = document.getElementById('line');
      if (sel && a.current_line) sel.value = a.current_line;
    }
  }

  const reason = document.getElementById('access-reason');
  reason.textContent = g.reason || '';
  reason.hidden = !g.reason;

  const probs = document.getElementById('access-problems');
  const items = a.problems || [];
  probs.textContent = items.length ? `アクセス権限の表で気になる行:\n・${items.join('\n・')}` : '';
  probs.hidden = !items.length;

  // 当てはまった行。**ほかのツールの権限も並べる**(このツールでは使わない、と添える)
  const body = document.getElementById('access-matched');
  body.innerHTML = '';
  for (const r of g.matched || []) {
    const tr = document.createElement('tr');
    for (const text of [r.no, r.condition, r.permission, r.meaning]) {
      const td = document.createElement('td');
      td.textContent = text === null || text === undefined ? '' : String(text);
      tr.appendChild(td);
    }
    if (!r.known) tr.className = 'access-other';
    body.appendChild(tr);
  }
  document.getElementById('access-matched-wrap').hidden = !(g.matched || []).length;

  const src = document.getElementById('access-source');
  const how = { fresh: 'いま読みました', cache: '読めないので、前回読めた内容です', none: '読めませんでした' }[g.source] || '';
  src.textContent = `${a.path || g.path || ''}${how ? ` ── ${how}` : ''}${g.read_at ? `(${g.read_at})` : ''}`;

  // モードの選択肢。**いまのモードは選べるまま**(選び直しで元に戻せるように)
  const select = document.getElementById('mode');
  if (select) {
    for (const o of select.options) {
      const m = g.modes.find((x) => x.key === o.value);
      if (!m) continue;
      const port = (state.modes.find((x) => x.key === o.value) || {}).port || '';
      const base = labelOf(o.value) + (desktop.isDesktop ? '' : `(ポート ${port})`);
      o.disabled = !m.allowed && o.value !== state.mode;
      o.textContent = base + (m.allowed ? '' : '(権限なし)');
    }
  }
}

// ------------------------------------------------------------------
// 看板履歴の置き場所(kanban/db/history.py)・CSV の書き出し先
// ------------------------------------------------------------------
function wireHistoryPath() {
  if (!document.getElementById('history-path-card')) return;
  checkHistory();
  on('history-save', async () => {
    const path = document.getElementById('history-path').value.trim();
    const creds = await askPassword(path
      ? `看板履歴の置き場所を ${path} にします。`
      : '看板履歴の置き場所を既定(共有DBと同じフォルダ)に戻します。');
    if (!creds) return;
    const box = document.getElementById('history-path-result');
    try {
      const r = await api.post('/api/history-db-path', { path, ...creds });
      box.className = 'banner banner--ok';
      box.textContent = `✔ ${clock()} ${r.message}`;
      box.hidden = false;
      renderHistory(r);
    } catch (e) {
      box.className = 'banner banner--bad';
      box.textContent = e.message;
      box.hidden = false;
      bad(e.message);
    }
  });
}

async function checkHistory() {
  try {
    renderHistory(await api.get('/api/history-db-status'));
  } catch (e) {
    const el = document.getElementById('history-now-state');
    el.className = 'source-state is-bad';
    el.textContent = `確かめられませんでした: ${e.message}`;
  }
}

function renderHistory(h) {
  state.history = { ...(state.history || {}), ...h };
  document.getElementById('history-now-path').textContent = h.path || '';
  const el = document.getElementById('history-now-state');
  if (h.problem) {
    el.className = 'source-state is-bad';
    el.textContent = `✖ 使えません ── ${String(h.problem).split('\n').join(' ')}`;
  } else if (!h.exists) {
    el.className = 'source-state is-unset';
    el.textContent = 'まだありません(最初に記録を送るとき・起動したときに作ります)';
  } else {
    el.className = 'source-state is-ok';
    el.textContent = `✔ 届いています(${h.rows === null || h.rows === undefined ? '?' : h.rows} 件${h.configured ? '' : ' / 既定の場所'})`;
  }
}

// 押し間違いとみなす時間(看板集計)。保存したら集計を読み直す
function wireMistake() {
  on('mistake-save', async () => {
    const minutes = document.getElementById('mistake-minutes').value.trim();
    const creds = await askPassword(`押し間違いとみなす時間を ${minutes} 分にします。`);
    if (!creds) return;
    const box = document.getElementById('mistake-result');
    try {
      const r = await api.post('/api/stats/mistake-minutes', { minutes, ...creds });
      box.className = 'banner banner--ok';
      box.textContent = `✔ ${clock()} ${r.message}`;
      box.hidden = false;
      document.getElementById('stats-run').click();
    } catch (e) {
      box.className = 'banner banner--bad';
      box.textContent = e.message;
      box.hidden = false;
      bad(e.message);
    }
  });
}

// 記録(ログ)の置き場所(app/routes/trace.py)。変えたらその場で書き先が切り替わる
function wireLogDir() {
  if (!document.getElementById('log-dir-card')) return;
  const show = (d) => {
    state.log_dir = d.path;
    document.getElementById('log-now-path').textContent = d.path;
    document.getElementById('log-now-file').textContent = d.text_log || '(まだありません)';
    document.getElementById('log-keep-days').textContent = d.keep_days;
    const input = document.getElementById('log-dir');
    input.placeholder = d.configured ? '' : d.path;
    if (d.configured && !input.value) input.value = d.path;
  };
  api.get('/api/log-dir').then(show).catch(() => {});
  on('log-save', async () => {
    const path = document.getElementById('log-dir').value.trim();
    const creds = await askPassword(path
      ? `記録(ログ)の置き場所を ${path} にします。`
      : '記録(ログ)の置き場所を既定(この端末のローカル領域)に戻します。');
    if (!creds) return;
    const box = document.getElementById('log-dir-result');
    try {
      const r = await api.post('/api/log-dir', { path, ...creds });
      show(r);
      box.className = 'banner banner--ok';
      box.textContent = `✔ ${clock()} ${r.message}`;
      box.hidden = false;
    } catch (e) {
      box.className = 'banner banner--bad';
      box.textContent = e.message;
      box.hidden = false;
      bad(e.message);
    }
  });
}

function wireCsvDir() {
  if (!document.getElementById('csv-dir-card')) return;
  on('csv-save', async () => {
    const path = document.getElementById('csv-dir').value.trim();
    const creds = await askPassword(path
      ? `CSV の書き出し先を ${path} にします。`
      : 'CSV の書き出し先を既定(この端末のローカル領域)に戻します。');
    if (!creds) return;
    const box = document.getElementById('csv-dir-result');
    try {
      const r = await api.post('/api/csv-dir', { path, ...creds });
      state.csv_dir = r.path;
      document.getElementById('csv-now-path').textContent = r.path;
      const dest = document.getElementById('csv-dest');
      if (dest) dest.textContent = r.path;
      box.className = 'banner banner--ok';
      box.textContent = `✔ ${clock()} ${r.message}`;
      box.hidden = false;
    } catch (e) {
      box.className = 'banner banner--bad';
      box.textContent = e.message;
      box.hidden = false;
      bad(e.message);
    }
  });
}

/** 「アクセス権限を直す」: マスタ管理の面へ移って、アクセス権限の表を開く */
async function openAccessTable() {
  const tab = document.querySelector('.tabs--settings .tab[data-tab="master"]');
  if (tab) tab.click();
  await loadMaster();
  const select = document.getElementById('master-table');
  const opt = [...select.options].find((o) => o.dataset.db === 'access');
  if (!opt || opt.disabled) { bad(opt ? opt.title : 'アクセス権限の表が見つかりません'); return; }
  select.value = opt.value;
  openTable(0);
}

/** アクセス権限の置き場所と、使えるか */
function renderAccessPath(a) {
  const pathEl = document.getElementById('access-now-path');
  const el = document.getElementById('access-now-state');
  if (!pathEl || !el) return;
  pathEl.textContent = a.path || '';
  if (a.path_problem) {
    el.className = 'source-state is-bad';
    el.textContent = `✖ 使えません ── ${String(a.path_problem).split('\n').join(' ')}`;
  } else if (!a.table_exists) {
    el.className = 'source-state is-unset';
    el.textContent = '✔ 届いています(アクセス権限の表はまだありません ── マスタ管理を開くと作ります)';
  } else {
    el.className = 'source-state is-ok';
    el.textContent = `✔ 届いています(アクセス権限 ${a.rows} 行${a.configured ? '' : ' / 既定の場所'})`;
  }
}

async function onAccessPathChange() {
  const input = document.getElementById('access-path');
  const path = input.value.trim();
  const creds = await askPassword(path
    ? `アクセス権限の置き場所を ${path} にします。`
    : 'アクセス権限の置き場所を既定(共有DBと同じフォルダ)に戻します。');
  if (!creds) return;
  const box = document.getElementById('access-path-result');
  try {
    const r = await api.post('/api/access-db-path', { path, ...creds });
    box.className = 'banner banner--ok';
    box.textContent = `✔ ${clock()} ${r.message}`;
    box.hidden = false;
    refreshAccess();
  } catch (e) {
    box.className = 'banner banner--bad';
    box.textContent = e.message;
    box.hidden = false;
    bad(e.message);
  }
}

function labelOf(key) {
  const m = state.modes.find((x) => x.key === key);
  return m ? m.label : key;
}

function labelOfLine(code) {
  const l = (state.lines || []).find((x) => x.code === code);
  return l ? l.label : code;
}

// ------------------------------------------------------------------
// 担当ライン
// ------------------------------------------------------------------
async function onLineChange(ev) {
  const next = ev.target.value;
  if (next === state.line) return;

  // 担当ラインを変えると、取り込むラインと書き戻すラインが入れ替わる。
  // 現場の端末が別ラインを指したまま操作されると、そのラインの発注が
  // 本来の担当者の知らないところで動く
  const creds = await askPassword(
    `担当ラインを「${labelOfLine(next)}」に変えます。`);
  if (!creds) { ev.target.value = state.line; return; }

  try {
    const r = await api.post('/api/line', { line: next, ...creds });
    state.line = r.line;
    ok(`担当ラインを ${r.label} にしました${r.imported ? `(${r.label} の看板を取り込みました)` : ''}。`);
    if (r.note) {
      // 表でラインが決まっている端末: 次に起動すると戻る。読み直したあとも見えるように残す
      warn(r.note);
      try { sessionStorage.setItem('kanban.settings.lineNote', r.note); } catch (e) { /* 残せなくてよい */ }
    }
    setTimeout(() => location.reload(), r.note ? 2500 : 900);
  } catch (e) {
    ev.target.value = state.line;
    bad(e.message);
  }
}

// ------------------------------------------------------------------
// 接続先
// ------------------------------------------------------------------
// **受け付けられたかが見えるように。** 以前は押した直後のトーストが 1 秒ほどで
// 消えて画面が読み直され、そのあと何も残らなかった ── もう一度押すと
// 「変更はありません」とだけ出て、変わったのかどうか分からなかった。
//
// ・「いまの接続先」と「届いているか」をいつも出す
// ・入力欄を書き換えると「まだ切り替わっていません」と出し、同じなら押せなくする
// ・結果はカードの中に残す。読み直したあとも出す(sessionStorage で渡す)
const SOURCE_RESULT = 'kanban.settings.sourceResult';

function wireSource() {
  const input = document.getElementById('accdb');
  if (!input) return;
  input.addEventListener('input', updateSourceEdit);
  updateSourceEdit();
  checkSource();

  // 読み直す前に残した結果(変えた直後だけ。古いものは出さない)
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(SOURCE_RESULT) || 'null');
    sessionStorage.removeItem(SOURCE_RESULT);
  } catch (e) { saved = null; }
  if (saved && Date.now() - saved.at < 5 * 60 * 1000) showSourceResult(saved.kind, saved.text);
}

/** 綴りの揺れ(大文字小文字・/ と \・末尾の区切り)を吸収する。サーバも同じ扱い */
function samePath(a, b) {
  const norm = (p) => String(p || '').trim().replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
  return norm(a) === norm(b);
}

function updateSourceEdit() {
  const input = document.getElementById('accdb');
  const note = document.getElementById('source-edit');
  const btn = document.getElementById('save-accdb');
  const same = samePath(input.value, state.shared_db_path);
  if (!input.value.trim()) {
    note.hidden = false;
    note.className = 'source-edit is-pending';
    note.textContent = 'パスが空です。';
    btn.disabled = true;
  } else if (same && state.shared_db_configured) {
    note.hidden = true;
    btn.disabled = true;
    btn.title = 'いまの接続先と同じです。変えるときは書き換えるか「参照...」で選んでください';
  } else if (same) {
    // 決めていない(既定の場所を使っている)。押すとこの場所に決める
    note.hidden = false;
    note.className = 'source-edit is-pending';
    note.textContent = 'まだ接続先を決めていません(既定の場所を使っています)。「変更」を押すとこの場所に決めます。';
    btn.disabled = false;
    btn.title = '';
  } else {
    note.hidden = false;
    note.className = 'source-edit is-pending';
    note.textContent = 'まだ切り替わっていません ── 「変更」を押すと、この場所に切り替えます。';
    btn.disabled = false;
    btn.title = '';
  }
}

/** いまの接続先と、届いているか(読むだけ。パスワードは要らない) */
async function checkSource() {
  const el = document.getElementById('source-now-state');
  if (!el) return;
  el.className = 'source-state';
  el.textContent = '確かめています…';
  try {
    const st = await api.get('/api/shared-db-status');
    state.shared_db_path = st.path;
    state.shared_db_configured = st.configured;
    document.getElementById('source-now-path').textContent = st.path;
    if (st.problem) {
      el.className = 'source-state is-bad';
      el.textContent = `✖ 使えません ── ${st.problem.split('\n').join(' ')}`;
    } else if (!st.configured) {
      el.className = 'source-state is-unset';
      el.textContent = '✔ 届いています(まだ決めていません ── 既定の場所を使っています)';
    } else {
      el.className = 'source-state is-ok';
      el.textContent = '✔ 届いています';
    }
    updateSourceEdit();
  } catch (e) {
    el.className = 'source-state is-bad';
    el.textContent = `確かめられませんでした: ${e.message}`;
  }
}

function showSourceResult(kind, text) {
  const box = document.getElementById('source-result');
  if (!box) return;
  box.className = `banner banner--${kind}`;
  box.textContent = text;
  box.hidden = false;
}

function clock() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

async function onAccdbChange() {
  const input = document.getElementById('accdb');
  const path = input.value.trim();
  if (!path) { warn('パスが空です'); return; }
  if (samePath(path, state.shared_db_path) && state.shared_db_configured) {
    showSourceResult('warn', 'いまの接続先と同じ場所です。変わっていません。\n'
      + '変えるときは、上の欄を書き換えるか「参照...」で選んでから「変更」を押してください。');
    return;
  }

  const creds = await askPassword(
    '接続先を変えます。本番データの居場所そのものなので、綴りを確かめてください。'
  );
  if (!creds) return;

  const btn = document.getElementById('save-accdb');
  btn.disabled = true;
  showSourceResult('warn', `切り替えています… ${path}`);
  try {
    const r = await api.post('/api/shared-db-path', { path, ...creds });
    if (!r.changed) {
      showSourceResult(r.problem ? 'bad' : 'warn', r.message);
      return;
    }
    state.shared_db_path = r.path;
    state.shared_db_configured = true;
    const text = `✔ 接続先を変えました(${clock()})\n${r.path}\n${r.message || ''}`.trim();
    const kind = r.reconnected ? 'ok' : 'warn';
    showSourceResult(kind, text);
    ok('接続先を変えました');
    // **開き直しは要らない。** その場で繋ぎ直して取り込んである。画面の
    // 数字(取り込み済み・未反映)を新しい接続先で出し直すために読み直す。
    // 結果は読み直したあとも同じ場所に出す
    try {
      sessionStorage.setItem(SOURCE_RESULT, JSON.stringify({ kind, text, at: Date.now() }));
    } catch (e) { /* 残せなくても、いまは出ている */ }
    if (r.reconnected) setTimeout(() => location.reload(), 1500);
    else checkSource();
  } catch (e) {
    showSourceResult('bad', `✖ 変えられませんでした(接続先は元のままです)\n${e.message}`);
    bad(e.message);
  } finally {
    updateSourceEdit();
  }
}

// ------------------------------------------------------------------
// 取り込み
// ------------------------------------------------------------------
// 「開き直してください」と言われても、窓の無いアプリでは開き直せない
// ことがある。**取り込み直す手段を画面に置く。**
async function onReimport() {
  const btn = document.getElementById('reimport');
  const creds = await askPassword('共有DBから取り込み直します。');
  if (!creds) return;

  btn.disabled = true;
  const before = btn.textContent;
  btn.textContent = '↻ 取り込み中…';
  try {
    const r = await api.post('/api/reimport', creds);
    if (r.failed_lines) warn(r.message);
    else ok(r.message);
    setTimeout(() => location.reload(), 1200);
  } catch (e) {
    bad(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = before;
  }
}

// ------------------------------------------------------------------
// パスワードの覆い
// ------------------------------------------------------------------
// まだ設定されていない端末では、**最初の1回だけ**登録を求める
// (tkinter 版の admin_auth と同じ)。
//
// **認証済みなら訊かない。** 開いているかはサーバに確かめる(時間が経って
// 閉まっていることがある)。パスワードを入れて操作が通ると認証済みになるので、
// そのあと帯の表示を取り直す。
async function askPassword(note) {
  if (state.has_admin_password) {
    try {
      const st = await api.get('/api/admin/state');
      applyLock(st);
      if (st.admin_unlocked) return {};
    } catch (e) { /* 確かめられなければ訊く */ }
  }
  const creds = await promptPassword(note);
  if (creds) setTimeout(refreshLock, 1500);
  return creds;
}

function promptPassword(note) {
  return new Promise((resolve) => {
    const box = document.getElementById('pw');
    const input = document.getElementById('pw-input');
    const confirmField = document.getElementById('pw-confirm-field');
    const confirmInput = document.getElementById('pw-confirm');
    const err = document.getElementById('pw-error');
    const setup = !state.has_admin_password;

    document.getElementById('pw-note').textContent = setup
      ? `${note} 管理者パスワードがまだ設定されていません。ここで新しく設定します。`
      : note;
    confirmField.hidden = !setup;
    input.value = '';
    confirmInput.value = '';
    err.hidden = true;
    box.hidden = false;
    input.focus();

    const done = (value) => {
      box.hidden = true;
      cleanup();
      resolve(value);
    };

    const onOk = () => {
      if (!input.value) {
        err.textContent = 'パスワードを入力してください';
        err.hidden = false;
        return;
      }
      if (setup && input.value !== confirmInput.value) {
        err.textContent = '確認用のパスワードが一致しません';
        err.hidden = false;
        return;
      }
      // 登録できたら、次からは確認欄を出さない
      if (setup) state.has_admin_password = true;
      done(setup
        ? { password: input.value, password_confirm: confirmInput.value }
        : { password: input.value });
    };

    const onKey = (ev) => {
      if (ev.key === 'Enter') onOk();
      if (ev.key === 'Escape') done(null);
    };

    const okBtn = document.getElementById('pw-ok');
    const cancelBtn = document.getElementById('pw-cancel');
    const onCancel = () => done(null);

    function cleanup() {
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      box.removeEventListener('keydown', onKey);
    }

    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    box.addEventListener('keydown', onKey);
  });
}

// ==================================================================
// フォルダ参照(サーバ側)
// ==================================================================
// ブラウザのファイル選択ダイアログは**クライアント側**のパスしか返さない。
// アプリが読むのはサーバ(=このPC)から見たパスなので、選ばせても意味が
// 無い。代わりに `/api/fs/list` でサーバ側のフォルダを一覧する。

// フォルダ参照はこの PC のフォルダ構成を返すので、**マスタの読み取り以外**と
// 同じく管理者パスワードが要る。ただし毎回訊くとフォルダを 1 つ辿るたびに
// 入力させることになるので、**「参照...」を開いているあいだだけ**手元に持ち、
// 毎回サーバへ送る(サーバ側に解錠状態は持たない)。閉じたら捨てる。
let browseCreds = null;
let browseTarget = 'source';   // 一覧で選んだファイルの行き先(接続先 / 中身を入れ替える / アクセス権限)

function wireBrowser() {
  const box = document.getElementById('fs');
  if (!box) return;
  const input = document.getElementById('accdb');

  // 一覧は 1 つを使い回す。開いた側(接続先 / 中身を入れ替える)のカードの中へ動かす
  const home = { parent: box.parentNode, next: box.nextSibling };
  const openFor = async (target, slot, start) => {
    if (!box.hidden && browseTarget === target) { closeBrowser(); return; }
    // デスクトップ版は OS の標準のダイアログで選ぶ(ネットワークのフォルダも辿れる)。
    // フォルダの一覧を Python が返すわけではないので、パスワードは訊かない
    // (選んだパスを保存する「変更」で訊く)。開けなかったときだけ下の一覧へ
    if (desktop.isDesktop && await pickNative(target, start)) return;
    const creds = await askPassword('この PC のフォルダを一覧します。');
    if (!creds) return;
    browseCreds = creds;
    browseTarget = target;
    if (slot) slot.appendChild(box); else home.parent.insertBefore(box, home.next);
    box.hidden = false;
    // いま入っているパスの場所から始める。空なら出発点だけが出る
    loadDir(start);
  };
  on('browse', () => openFor('source', null, input.value.trim()));
  on('rf-browse', () => openFor('refresh', document.getElementById('rf-fs-slot'),
    document.getElementById('rf-path').value.trim()));
  on('access-browse', () => openFor('access', document.getElementById('access-fs-slot'),
    document.getElementById('access-path').value.trim()
      || (state.access && state.access.path) || ''));
  on('history-browse', () => openFor('history', document.getElementById('history-fs-slot'),
    document.getElementById('history-path').value.trim()
      || (state.history && state.history.path) || ''));
  on('csv-browse', () => openFor('csv', document.getElementById('csv-fs-slot'),
    document.getElementById('csv-dir').value.trim() || state.csv_dir || ''));
  on('log-browse', () => openFor('log', document.getElementById('log-fs-slot'),
    document.getElementById('log-dir').value.trim() || state.log_dir || ''));
  // フォルダを選ぶ: CSV はそのフォルダ、看板履歴はそのフォルダの 看板履歴.sqlite3
  on('fs-pick-dir', () => {
    const dir = document.getElementById('fs-path').value.trim();
    if (!dir) return;
    pickDir(dir);
  });
  on('fs-close', closeBrowser);
  on('fs-go', () => loadDir(document.getElementById('fs-path').value.trim()));
  on('fs-up', () => loadDir(box.dataset.parent || ''));
  document.getElementById('fs-path').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') loadDir(ev.target.value.trim());
  });
}

/** フォルダを選んだ(一覧の「このフォルダにする」・デスクトップ版の標準のダイアログ)。 */
function pickDir(dir) {
  if (browseTarget === 'csv') {
    document.getElementById('csv-dir').value = dir;
  } else if (browseTarget === 'log') {
    document.getElementById('log-dir').value = dir;
  } else if (browseTarget === 'history') {
    const sep = dir.includes('\\') ? '\\' : '/';
    document.getElementById('history-path').value = dir.replace(/[\\/]+$/, '') + sep + '看板履歴.sqlite3';
  }
  closeBrowser();
  ok('選びました。「変更」を押すと保存します。');
}

/**
 * デスクトップ版: OS の標準のダイアログで選ぶ。**選び終えたら(やめても)true**。
 * 外枠に頼めなかったら false(ブラウザ版と同じ一覧で選んでもらう)。
 */
const NATIVE_PICK = {
  source: { kind: 'file', title: '接続先(看板マスタ)を選ぶ', extensions: ['sqlite3', 'accdb'] },
  refresh: { kind: 'file', title: '入れ替える元の Access を選ぶ', extensions: ['accdb', 'sqlite3'] },
  access: { kind: 'file', title: '梱包資材マスタを選ぶ', extensions: ['sqlite3'] },
  history: { kind: 'folder', title: '看板履歴.sqlite3 を置くフォルダを選ぶ' },
  csv: { kind: 'folder', title: 'CSV の出力先を選ぶ' },
  log: { kind: 'folder', title: '記録・ログの置き場所を選ぶ' },
};

async function pickNative(target, start) {
  const how = NATIVE_PICK[target];
  if (!how) return false;
  let picked;
  try {
    picked = await desktop.pickPath(how.kind, { start, title: how.title, extensions: how.extensions || [] });
  } catch (e) {
    warn(`標準のダイアログを開けませんでした(${e && e.message ? e.message : e})。一覧から選んでください。`);
    return false;
  }
  if (!picked) return true;   // やめた
  browseTarget = target;
  if (how.kind === 'folder') pickDir(picked);
  else pickFile(picked);
  return true;
}

function closeBrowser() {
  document.getElementById('fs').hidden = true;
  // **閉じたら捨てる。** 開きっぱなしのタブが解錠のまま残らないようにする
  browseCreds = null;
}

async function loadDir(path) {
  const box = document.getElementById('fs');
  const list = document.getElementById('fs-list');
  const msg = document.getElementById('fs-message');
  if (!browseCreds) { closeBrowser(); return; }
  try {
    const v = await api.post('/api/fs/list', { path: path || '', ...browseCreds });
    box.dataset.parent = v.parent || '';
    document.getElementById('fs-path').value = v.path || '';
    document.getElementById('fs-up').disabled = !v.parent;

    // 中身の入れ替えでは Access をそのまま選ぶ(「変換してください」は言わない)
    const note = browseTarget === 'refresh'
      ? (v.files && v.files.length ? 'Access(.accdb)か、変換した sqlite3 を選んでください。' : v.message)
      : browseTarget === 'access'
        ? (v.files && v.files.length ? '梱包資材マスタ(梱包資材マスタ.sqlite3)を選んでください。' : v.message)
        : v.message;
    document.getElementById('fs-pick-dir').hidden = !['csv', 'history', 'log'].includes(browseTarget);
    msg.textContent = note || '';
    msg.hidden = !note;
    msg.className = 'banner' + (v.exists ? '' : ' banner--warn');

    // 出発点(Windows のドライブなど)。「どこから始めればよいか」を作らない
    const roots = document.getElementById('fs-roots');
    roots.innerHTML = '';
    for (const r of v.roots || []) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn--sm';
      b.textContent = r.name;
      b.addEventListener('click', () => loadDir(r.path));
      roots.appendChild(b);
    }

    list.innerHTML = '';
    for (const d of v.dirs || []) list.appendChild(fsItem(d, '📁', () => loadDir(d.path)));
    for (const f of v.files || []) {
      if (f.legacy && browseTarget !== 'refresh') {
        // あることは見せるが選ばせない(変換がまだ、と分かるように)
        const el = fsItem(f, '⚠', null);
        el.classList.add('fs-item--legacy');
        el.title = '変換前の Access ファイルです。tools/accdb_to_sqlite.py で変換してください。';
        el.disabled = true;
        list.appendChild(el);
      } else {
        const el = fsItem(f, f.legacy ? '📒' : '🗄', () => pickFile(f.path));
        if (f.name === v.picked) el.classList.add('fs-item--picked');
        list.appendChild(el);
      }
    }
    if (!list.children.length) {
      list.innerHTML = '<div style="padding:.6rem;color:var(--muted)">表示するものがありません。</div>';
    }
  } catch (e) {
    bad(e.message);
    // パスワードが通らなくなったら開いたままにしない
    if (e.status === 401 || e.status === 403) closeBrowser();
  }
}

function fsItem(entry, glyph, onClick) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = 'fs-item';
  el.innerHTML = `<span class="gl">${glyph}</span><span></span>`;
  el.lastChild.textContent = entry.name;
  if (onClick) el.addEventListener('click', onClick);
  return el;
}

function pickFile(path) {
  if (browseTarget === 'refresh') {
    document.getElementById('rf-path').value = path;
    closeBrowser();
    refreshPlan();
    return;
  }
  if (browseTarget === 'history') {
    document.getElementById('history-path').value = path;
    closeBrowser();
    ok('選びました。「変更」を押すと保存します。');
    return;
  }
  if (browseTarget === 'csv' || browseTarget === 'log') return;   // フォルダを選ぶ(「このフォルダにする」)
  if (browseTarget === 'access') {
    // 選んだだけでは保存しない。**「変更」を押すまで効かない**
    document.getElementById('access-path').value = path;
    closeBrowser();
    ok('選びました。「変更」を押すと保存します。');
    return;
  }
  // 選んだだけでは保存しない。**「変更」を押すまで効かない**
  document.getElementById('accdb').value = path;
  closeBrowser();
  updateSourceEdit();
  ok('選びました。「変更」を押すと保存します。');
}

// ==================================================================
// Access の最新で表の中身を入れ替える(kanban/table_refresh.py)
// ==================================================================
// Access をまるごと変換して差し替えると、このツールが共有DBに足した表・列・行
// (看板履歴・看板コメント・看板の状態)が消える。両方にある表の中身だけを入れ替える。
let refreshPlanView = null;

function wireRefresh() {
  const drop = document.getElementById('rf-drop');
  if (!drop) return;
  const file = document.getElementById('rf-file');
  drop.addEventListener('click', () => file.click());
  drop.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') file.click(); });
  file.addEventListener('change', () => { if (file.files[0]) refreshUpload(file.files[0]); file.value = ''; });
  for (const type of ['dragenter', 'dragover']) {
    drop.addEventListener(type, (ev) => { ev.preventDefault(); drop.classList.add('is-over'); });
  }
  for (const type of ['dragleave', 'drop']) {
    drop.addEventListener(type, () => drop.classList.remove('is-over'));
  }
  drop.addEventListener('drop', (ev) => {
    ev.preventDefault();
    const f = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
    if (f) refreshUpload(f);
  });
  on('rf-plan', refreshPlan);
  document.getElementById('rf-path').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') refreshPlan();
  });
  on('rf-run', refreshRun);
}

async function refreshUpload(f) {
  const creds = await askPassword('選んだ Access のファイルを読みます。');
  if (!creds) return;
  const drop = document.getElementById('rf-drop');
  drop.classList.add('is-busy');
  showRefreshNote('', `受け取って読んでいます… ${f.name}(${sizeText(f.size)})`);
  document.getElementById('rf-compare').hidden = true;
  try {
    const form = new FormData();
    form.append('file', f);
    for (const [k, v] of Object.entries(creds)) form.append(k, v);
    const r = await api.upload('/api/table-refresh/upload', form);
    document.getElementById('rf-path').value = r.path;
    renderRefreshPlan(r.plan);
  } catch (e) {
    showRefreshNote('bad', e.message);
  } finally {
    drop.classList.remove('is-busy');
  }
}

async function refreshPlan() {
  const path = document.getElementById('rf-path').value.trim();
  if (!path) { showRefreshNote('warn', 'Access のファイルを落とすか、場所を入れてください。'); return; }
  const creds = await askPassword('選んだ Access のファイルを読みます。');
  if (!creds) return;
  showRefreshNote('', `読んでいます… ${path}`);
  document.getElementById('rf-compare').hidden = true;
  try {
    const r = await api.post('/api/table-refresh/plan', { path, ...creds });
    renderRefreshPlan(r.plan);
  } catch (e) {
    showRefreshNote('bad', e.message);
  }
}

function showRefreshNote(kind, text) {
  const note = document.getElementById('rf-note');
  note.className = 'banner' + (kind ? ` banner--${kind}` : '');
  note.textContent = text;
  note.hidden = !text;
}

function sizeText(bytes) {
  if (!bytes) return '0 バイト';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024)).toLocaleString()} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function factsText(f, kind) {
  if (!f || !f.path) return '';
  return `${f.path}\n  (${kind ? kind + '・' : ''}${sizeText(f.size)}・更新 ${f.modified || '不明'}`
    + `・表 ${f.tables} 個、うち看板の表 ${f.kanban_tables.length} 個)`;
}

// 両方の表の名前を並べる。「0 個」と出たときに、**何と何を比べたのか**を自分の目で確かめられるように
function renderRefreshCompare(plan) {
  const box = document.getElementById('rf-compare');
  const cols = document.getElementById('rf-compare-cols');
  cols.innerHTML = '';
  box.hidden = !plan.ok;
  if (!plan.ok) return;
  const kanban = new Set([...(plan.source_facts.kanban_tables || []), ...(plan.dest_facts.kanban_tables || [])]);
  const lists = [
    ['両方にある表(入れ替えの候補)', (plan.tables || []).map((t) => t.name)],
    [`選んだファイルにだけある表(${plan.source_facts.name})`, plan.only_in_source || []],
    ['共有DBにだけある表', plan.only_in_dest || []],
  ];
  for (const [title, names] of lists) {
    const col = document.createElement('div');
    const h = document.createElement('h4');
    h.textContent = `${title}: ${names.length} 個`;
    const ul = document.createElement('ul');
    for (const n of names) {
      const li = document.createElement('li');
      li.textContent = n;
      if (kanban.has(n)) li.className = 'is-kanban';
      ul.append(li);
    }
    col.append(h, ul);
    cols.append(col);
  }
  // 候補が無いときは開いておく(閉じていると「何も出ない」に見える)
  box.open = !(plan.tables || []).length;
}

function renderRefreshPlan(plan) {
  refreshPlanView = plan;
  // **読んだファイル**と**書き込み先**を両方出す(どのファイルを見て、どのファイルが変わるのかを
  // 押す前に確かめられる)。以前は書き込み先だけで、何を読んだのか分からなかった
  const lines = [];
  if (plan.ok) {
    lines.push(`読んだファイル: ${factsText(plan.source_facts, plan.access ? 'Access' : 'sqlite3')}`);
    lines.push(`書き込み先の共有DB: ${factsText(plan.dest_facts, '')}`);
  }
  lines.push(plan.message);
  if (plan.hint) lines.push(`\n⚠ ${plan.hint}`);
  showRefreshNote(!plan.ok ? 'bad' : (plan.hint ? 'warn' : ''), lines.join('\n'));
  renderRefreshCompare(plan);
  const wrap = document.getElementById('rf-list');
  const table = document.getElementById('rf-table');
  table.innerHTML = '';
  wrap.hidden = !plan.ok || !plan.tables.length;
  const head = document.createElement('tr');
  for (const h of ['入れ替える', '表', 'いま → Access', '説明']) {
    const th = document.createElement('th');
    th.textContent = h;
    head.append(th);
  }
  table.append(head);
  for (const t of plan.tables || []) {
    const tr = document.createElement('tr');
    if (!t.can_refresh) tr.className = 'is-off';
    const pick = document.createElement('td');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.dataset.table = t.name;
    box.disabled = !t.can_refresh;
    box.addEventListener('change', updateRefreshButton);
    pick.append(box);
    const name = document.createElement('td');
    name.textContent = t.name;
    const rows = document.createElement('td');
    rows.className = 'num';
    rows.textContent = `${t.current_rows.toLocaleString()} 行 → ${t.rows.toLocaleString()} 行`;
    const why = document.createElement('td');
    why.className = 'why';
    if (t.refresh_why) {
      const b = document.createElement('b');
      b.textContent = `入れ替えません: ${t.refresh_why}`;
      why.append(b);
    } else {
      const parts = [];
      if (t.preview) parts.push(t.preview);
      if (t.keeps) parts.push(t.keeps);
      if (t.not_copied.length) parts.push(`共有DBに無いので写さない列: ${t.not_copied.join('、')}`);
      why.textContent = parts.join(' / ') || '中身を Access の最新にします';
    }
    tr.append(pick, name, rows, why);
    table.append(tr);
  }
  updateRefreshButton();
}

function checkedRefreshTables() {
  return [...document.querySelectorAll('#rf-table input[data-table]:checked')].map((b) => b.dataset.table);
}

function updateRefreshButton() {
  const n = checkedRefreshTables().length;
  const btn = document.getElementById('rf-run');
  btn.disabled = !n;
  btn.textContent = n ? `選んだ表(${n})の中身を入れ替える` : '選んだ表の中身を入れ替える';
}

async function refreshRun() {
  const tables = checkedRefreshTables();
  if (!tables.length || !refreshPlanView) return;
  const lines = tables.map((name) => {
    const t = refreshPlanView.tables.find((x) => x.name === name);
    return `・${name}(いま ${t.current_rows} 行 → Access ${t.rows} 行)`;
  });
  if (!confirm('次の表の中身を Access の最新に入れ替えます。\n\n' + lines.join('\n')
    + '\n\n看板の表は、状態(赤・緑・注文中)はいまのまま残します。'
    + '\n書く前に共有DBの控えを取ります。よろしいですか？')) return;
  const creds = await askPassword('共有DBの表の中身を入れ替えます。');
  if (!creds) return;
  const btn = document.getElementById('rf-run');
  btn.disabled = true;
  const result = document.getElementById('rf-result');
  try {
    const r = await api.post('/api/table-refresh/run', {
      path: refreshPlanView.source, tables, ...creds,
    });
    result.className = 'banner banner--ok';
    // **書いたファイルを名指しする。** 以前は控え(入れ替える前の中身)の場所だけを
    // 出していたので、それを開いて「更新されていない」と見えた
    result.textContent = [
      `✔ ${r.message}`, ...r.notes,
      r.written ? `書き込んだ共有DB: ${r.written}(更新日時 ${r.written_at})` : '',
      r.verified, r.reimported,
      r.backup ? `書く前の控え(入れ替える前の中身。戻したいとき用): ${r.backup}` : '',
    ].filter(Boolean).join('\n');
    result.hidden = false;
    ok('中身を入れ替えました');
    renderRefreshPlan(r.plan);
    loadMaster();
  } catch (e) {
    result.className = 'banner banner--bad';
    result.textContent = `✖ ${e.message}`;
    result.hidden = false;
    if (e.body && e.body.plan) renderRefreshPlan(e.body.plan);
    bad(e.message);
  } finally {
    updateRefreshButton();
  }
}

// ==================================================================
// マスタ管理
// ==================================================================
// **パスが無ければ何も出さない。** 空表を出すと「マスタが消えた」に
// 見えるので、理由と次にすることを出す(サーバの `master.availability`)。

let masterView = null;

// 並べ替え。**表全体を**サーバで並べてからページに分ける(見えている 100 行だけ並べると、
// 次のページとつながらない)。列名を押すたびに 小さい順 → 大きい順 → 元の順(ファイルの順)
let masterSort = { table: '', db: '', column: '', dir: 'asc' };

function sortBy(v, column) {
  const same = masterSort.table === v.table && masterSort.db === (v.db || '') && masterSort.column === column;
  if (!same) masterSort = { table: v.table, db: v.db || '', column, dir: 'asc' };
  else if (masterSort.dir === 'asc') masterSort.dir = 'desc';
  else masterSort = { table: v.table, db: v.db || '', column: '', dir: 'asc' };
  openTable(0);
}

async function loadMaster() {
  const blocked = document.getElementById('master-blocked');
  const pick = document.getElementById('master-pick');
  if (!blocked) return;
  try {
    const v = await api.get('/api/master');
    masterView = v;
    const dest = document.getElementById('master-dest');
    if (dest) {
      dest.hidden = !v.path;
      document.getElementById('master-dest-path').textContent = v.path || '';
    }
    if (!v.available) {
      blocked.textContent = v.why;
      blocked.hidden = false;
      pick.hidden = true;
      document.getElementById('master-view').innerHTML = '';
      return;
    }
    blocked.hidden = true;
    pick.hidden = false;
    const select = document.getElementById('master-table');
    const before = select.value;
    const beforeDb = select.selectedOptions[0] ? select.selectedOptions[0].dataset.db || '' : '';
    select.innerHTML = '';
    for (const t of v.tables) {
      const o = document.createElement('option');
      o.value = t.name;
      o.textContent = t.name;
      o.dataset.db = '';
      select.appendChild(o);
    }
    // 梱包資材マスタの「アクセス権限」(共有DBとは別のファイル)。**使えなくても出して理由を添える**
    const extra = v.extra || [];
    if (extra.length) {
      const group = document.createElement('optgroup');
      group.label = '梱包資材マスタ';
      for (const x of extra) {
        const o = document.createElement('option');
        o.value = x.name;
        o.dataset.db = x.db;
        o.textContent = x.available ? x.label : `${x.label}(使えません)`;
        o.disabled = !x.available;
        o.title = x.available ? (x.path || '') : (x.why || '');
        group.appendChild(o);
        if (x.created) ok(x.created);
      }
      select.appendChild(group);
    }
    const again = [...select.options].find((o) => o.value === before && (o.dataset.db || '') === beforeDb);
    if (again) again.selected = true;
  } catch (e) {
    blocked.textContent = e.message;
    blocked.hidden = false;
  }
}

/** 選んでいる表がどのファイルのものか(``''`` = 共有DB / ``access`` = 梱包資材マスタ) */
function selectedDb() {
  const select = document.getElementById('master-table');
  const o = select && select.selectedOptions[0];
  return o ? (o.dataset.db || '') : '';
}

/** 書いた結果と**書き込み先**を残す(トーストは消えるので、確かめたいときに見えない) */
function showMasterWritten(r) {
  const box = document.getElementById('master-written');
  if (!box || !r) return;
  box.style.whiteSpace = 'pre-line';
  box.textContent = [
    `✔ ${clock()} ${r.message || '書き込みました'}`,
    r.written ? `書き込み先: ${r.written}${r.written_at ? `(更新日時 ${r.written_at})` : ''}` : '',
  ].filter(Boolean).join('\n');
  box.hidden = false;
  // アクセス権限を書いたら、この端末の権限とモードの選択肢も描き直す。
  // 行数など置き場所の状態(接続先タブ)は読み直して揃える
  if (r.access) {
    renderAccess(r.access);
    refreshAccess();
  }
}

/** 「最後のページ」を頼むときの番号。サーバが実際の最後のページに合わせる */
const LAST_PAGE = 1e9;

async function openTable(page) {
  const table = document.getElementById('master-table').value;
  if (!table) return;
  const db = selectedDb();
  // 並べ替えは同じ表を開き直したとき(ページ送り・直したあと)だけ引き継ぐ
  const sorted = masterSort.column && masterSort.table === table && masterSort.db === db;
  try {
    const v = await api.get(
      `/api/master/table?table=${encodeURIComponent(table)}&page=${page || 0}`
      + (db ? `&db=${encodeURIComponent(db)}` : '')
      + (sorted ? `&sort=${encodeURIComponent(masterSort.column)}&dir=${masterSort.dir}` : ''));
    masterView = v;
    closeAddForm();
    // **どのファイルを読み書きしているか**を、開いた表に合わせて出し直す
    const dest = document.getElementById('master-dest');
    if (dest && v.path) {
      dest.hidden = false;
      document.getElementById('master-dest-label').textContent = `読み書きする${v.db_label || '共有DB'}`;
      document.getElementById('master-dest-path').textContent = v.path;
    }
    renderTable(v);
    // 表が決まって初めて「足す」が押せる。隠さずに、押せない理由を出しておく
    const add = document.getElementById('master-add');
    if (add) {
      // 直せない表では押せない。**理由は表の上に出ている**ので、ここは
      // 同じ文を繰り返さず「押せない」ことだけ示す
      add.disabled = !v.table || !!v.view_only_why;
      add.title = !v.table ? '先にテーブルを開いてください'
        : v.view_only_why ? `${v.table} は直せません`
        : `${v.table} に 1 行足します`;
    }
  } catch (e) {
    bad(e.message);
  }
}

function renderTable(v) {
  const host = document.getElementById('master-view');
  host.innerHTML = '';
  if (!v.available || !v.table) return;

  // **直せない表は隠さず、理由を出す。**
  // 隠すと、探している人には画面が壊れて見える(「あるはずの表が無い」)。
  const readOnly = !!v.view_only_why;
  if (readOnly) {
    const why = document.createElement('div');
    why.className = 'banner banner--warn';
    why.style.marginTop = '.8rem';
    why.textContent = `この表は直せません。${v.view_only_why}`;
    host.appendChild(why);
  }
  // 表の読み方(アクセス権限: 空欄は問わない・ほかのツールの権限も入る、など)
  if (v.note) {
    const note = document.createElement('div');
    note.className = 'banner';
    note.style.marginTop = '.8rem';
    note.textContent = v.note;
    host.appendChild(note);
  }

  // **看板の状態は読むだけ。** 発注・発送はボタンが組にして書く(欲と不と
  // 更新日を同時に)ので、ここで 1 マス直すと組が崩れる
  const state = new Set((v.column_info || []).filter((c) => c.state).map((c) => c.name));
  if (!readOnly && state.size) {
    const note = document.createElement('p');
    note.className = 'master-note';
    note.textContent = `${[...state].join('・')} は看板の状態なので、ここでは直せません。`
      + '看板画面のボタンで操作してください。';
    host.appendChild(note);
  }

  const wrap = document.createElement('div');
  wrap.className = 'master-table-wrap';
  const table = document.createElement('table');
  table.className = 'master';

  const head = document.createElement('tr');
  for (const c of v.columns) {
    const th = document.createElement('th');
    // 列名を押すと並べ替え。キーボードでも押せるようにボタンにする
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'master-sort';
    const on = v.sort === c;
    btn.textContent = c + (on ? (v.sort_dir === 'desc' ? ' ▼' : ' ▲') : '');
    btn.title = !on ? `${c} の小さい順に並べ替え`
      : v.sort_dir === 'asc' ? `${c} の大きい順に並べ替え` : '元の順(ファイルの順)に戻す';
    btn.addEventListener('click', () => sortBy(v, c));
    th.appendChild(btn);
    if (on) th.setAttribute('aria-sort', v.sort_dir === 'desc' ? 'descending' : 'ascending');
    head.appendChild(th);
  }
  if (!readOnly) head.appendChild(document.createElement('th'));  // 削除ボタンの列
  table.appendChild(head);

  for (const row of v.rows) {
    const tr = document.createElement('tr');
    for (const c of v.columns) {
      const td = document.createElement('td');
      td.textContent = cellText(row[c]);
      if (readOnly) {
        // 押せる見た目にしない。押しても何も起きないと壊れて見える
        td.className = 'readonly';
      } else if (c === v.key_column) {
        // キー列は直せない。書き換えると、どの行だったのかが辿れなくなる。
        // 打ち間違えた行は「消して足し直す」── そのための削除ボタン
        td.className = 'key';
        td.title = 'キー列は変更できません(消して足し直してください)';
      } else if (state.has(c)) {
        td.className = 'readonly state';
        td.title = '看板の状態です。看板画面のボタンで操作してください';
      } else {
        td.className = 'editable';
        td.title = 'クリックで編集';
        td.addEventListener('click', () => editCell(td, v, row, c));
      }
      tr.appendChild(td);
    }
    if (!readOnly) {
      const act = document.createElement('td');
      act.className = 'master-act';
      const del = document.createElement('button');
      del.className = 'btn btn--sm btn--danger';
      del.type = 'button';
      del.textContent = '削除';
      del.title = 'この行を消します';
      del.addEventListener('click', () => deleteRow(v, row));
      act.appendChild(del);
      tr.appendChild(act);
    }
    table.appendChild(tr);
  }
  wrap.appendChild(table);
  host.appendChild(wrap);

  const pager = document.createElement('div');
  pager.className = 'master-pager';
  pager.innerHTML = `<span>${v.total} 件 / ${v.page + 1} ページ目(全 ${v.pages})</span>`;
  if (v.sort) {
    const note = document.createElement('span');
    note.textContent = `${v.sort} の${v.sort_dir === 'desc' ? '大きい' : '小さい'}順(空欄は最後)`;
    const reset = document.createElement('button');
    reset.className = 'btn btn--sm';
    reset.type = 'button';
    reset.textContent = '元の順に戻す';
    reset.addEventListener('click', () => {
      masterSort = { table: v.table, db: v.db || '', column: '', dir: 'asc' };
      openTable(0);
    });
    pager.append(note, reset);
  }
  if (v.pages > 1) {
    const prev = document.createElement('button');
    prev.className = 'btn btn--sm';
    prev.textContent = '前へ';
    prev.disabled = v.page <= 0;
    prev.addEventListener('click', () => openTable(v.page - 1));
    const next = document.createElement('button');
    next.className = 'btn btn--sm';
    next.textContent = '次へ';
    next.disabled = v.page >= v.pages - 1;
    next.addEventListener('click', () => openTable(v.page + 1));
    pager.appendChild(prev);
    pager.appendChild(next);
  }
  host.appendChild(pager);
}

/**
 * 入力候補(アクセス権限の「権限」など)。**候補以外も入れられる** ── ほかのツールの
 * 権限も入る表なので、選ばせるだけにはしない。
 */
function attachChoices(input, v, column) {
  const list = (v.choices || {})[column];
  if (!list || !list.length) return null;
  const dl = document.createElement('datalist');
  dl.id = `choices-${Math.random().toString(36).slice(2)}`;
  for (const value of list) {
    const o = document.createElement('option');
    o.value = value;
    dl.appendChild(o);
  }
  input.setAttribute('list', dl.id);
  return dl;
}

function editCell(td, v, row, column) {
  if (td.querySelector('input')) return;
  const before = td.textContent;
  const input = document.createElement('input');
  input.value = before;
  td.textContent = '';
  td.appendChild(input);
  const dl = attachChoices(input, v, column);
  if (dl) td.appendChild(dl);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const after = input.value;
    td.textContent = before;
    if (!save || after === before) return;

    // **直すたびに訊く。** 解錠状態を持つと、開きっぱなしのタブが
    // 解錠のまま残る(設定画面の他の保護された操作と同じ考え方)
    const creds = await askPassword(
      `${v.table} の「${column}」を「${after}」に変えます。`);
    if (!creds) return;
    try {
      // 見ていたキーも送る。開いたあとに行が入れ替わっていたら、別の行を
      // 直さずに断ってもらう(サーバが確かめる)
      const r = await api.post('/api/master/update', {
        table: v.table, row_key: row[v.row_key], key: row[v.key_column],
        column, value: after, db: v.db || '', ...creds,
      });
      ok('更新しました');
      showMasterWritten(r);
      // **入れた文字ではなく、入った値を出す。** サーバは前後の空白を落とし、
      // 数値の「05」は 5 に、空欄の数値は NULL にする。打った文字のまま
      // 出すと、共有DBと画面が食い違ったまま次の操作に進むことになる
      openTable(v.page);
    } catch (e) {
      bad(e.message);
      if (e.status === 409) openTable(v.page);   // 他端末が先に変えていた
    }
  };

  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') finish(true);
    if (ev.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
}

function cellText(value) {
  return value === null || value === undefined ? '' : String(value);
}

// ------------------------------------------------------------------
// 行を足す(= 看板を 1 枚増やす)
// ------------------------------------------------------------------
// **直すだけでは看板を増やせない。** 増やせないと結局 Access を開くことに
// なり、その Access をやめたのだから、ここに無いと運用が回らない。

function closeAddForm() {
  const host = document.getElementById('master-form');
  if (host) host.innerHTML = '';
}

function showAddForm() {
  const v = masterView;
  const host = document.getElementById('master-form');
  if (!host || !v || !v.table) return;
  if (host.querySelector('form')) { closeAddForm(); return; }   // もう一度押したら閉じる

  const form = document.createElement('form');
  form.className = 'master-form';
  // **表の名前は共有DBから来た値。** 組み立てた HTML に混ぜない ──
  // ここだけが innerHTML に外から来た文字を入れていた
  const heading = document.createElement('h3');
  heading.textContent = `${v.table} に 1 行足す`;
  form.appendChild(heading);
  // **看板の状態の欄は出さない。** 選べない欄を並べても入力の邪魔になるだけ
  // なので、何で足すのかを 1 行で言う
  const stateCols = (v.column_info || []).filter((c) => c.state).map((c) => c.name);
  if (stateCols.length) {
    const note = document.createElement('p');
    note.className = 'master-note';
    note.textContent = `看板の状態(${stateCols.join('・')})は入力しません。`
      + '「まだ発注していない」状態(不 = 〇)で足します。';
    form.appendChild(note);
  }

  const grid = document.createElement('div');
  grid.className = 'master-form__grid';
  for (const col of v.column_info) {
    if (col.state) continue;   // 看板の状態はサーバが決まった値で入れる
    const field = document.createElement('div');
    field.className = 'field';

    const label = document.createElement('label');
    label.setAttribute('for', `new-${col.name}`);
    label.textContent = col.name + (col.required ? ' *' : '');
    field.appendChild(label);

    const input = document.createElement('input');
    input.type = 'text';
    input.id = `new-${col.name}`;
    input.dataset.column = col.name;
    input.spellcheck = false;
    // キーは当てて入れておく(看板は連番)。直せる ── 枝番の運用を塞がない
    if (col.is_key) input.value = v.next_key || '';
    else if (v.defaults && v.defaults[col.name]) input.value = v.defaults[col.name];
    if (col.category === 'DATE') input.placeholder = '2026/08/31 09:00:00';
    field.appendChild(input);
    const dl = attachChoices(input, v, col.name);
    if (dl) field.appendChild(dl);

    const hint = col.is_key ? 'あとから変えられません(消して足し直しになります)'
      : (v.hints || {})[col.name] || '';
    if (hint) {
      const small = document.createElement('small');
      small.textContent = hint;
      field.appendChild(small);
    }
    grid.appendChild(field);
  }
  form.appendChild(grid);

  const actions = document.createElement('div');
  actions.className = 'modal__actions';
  const cancel = document.createElement('button');
  cancel.className = 'btn';
  cancel.type = 'button';
  cancel.textContent = 'やめる';
  cancel.addEventListener('click', closeAddForm);
  const save = document.createElement('button');
  save.className = 'btn btn--primary';
  save.type = 'submit';
  save.textContent = '足す';
  actions.appendChild(cancel);
  actions.appendChild(save);
  form.appendChild(actions);

  form.addEventListener('submit', (ev) => { ev.preventDefault(); submitAddForm(form, v, save); });
  host.appendChild(form);
  const first = form.querySelector('input');
  if (first) first.focus();
}

async function submitAddForm(form, v, save) {
  const values = {};
  for (const input of form.querySelectorAll('input[data-column]')) {
    values[input.dataset.column] = input.value;
  }

  const key = values[v.key_column];
  const creds = await askPassword(
    `${v.table} に ${v.key_column} ${key} の行を足します。`);
  if (!creds) return;

  save.disabled = true;
  try {
    const r = await api.post('/api/master/insert',
      { table: v.table, values, db: v.db || '', ...creds });
    ok(r.message || '足しました');
    showMasterWritten(r);
    closeAddForm();
    // 足した行は最後のページに出る。**足す前のページ数では足りない** ──
    // ちょうど 100 行だった表では、足した行は次のページにできる。
    // 大きな番号を頼めば、サーバが最後のページに合わせる
    openTable(LAST_PAGE);
  } catch (e) {
    bad(e.message);
  } finally {
    save.disabled = false;
  }
}

// ------------------------------------------------------------------
// 行を消す
// ------------------------------------------------------------------
// **取り返しがつかないので、2 段構え。** 何を消すのかを見せて確かめてから、
// 管理者パスワードを訊く(パスワードだけでは「何を消すか」が確認にならない)。
async function deleteRow(v, row) {
  const key = row[v.key_column];
  const label = v.columns
    .filter((c) => c !== v.key_column && cellText(row[c]))
    .slice(0, 2)
    .map((c) => cellText(row[c]))
    .join(' / ');
  const what = label ? `${v.key_column} ${key}(${label})` : `${v.key_column} ${key}`;
  // **いま動いている看板なら、そう言う。** 発注中の看板を消すと、倉庫からも
  // その発注が見えなくなる(黙って消えた発注になる)
  const on = (c) => cellText(row[c]).trim() === '〇';
  const busy = [on('欲') && '発注中', on('保留') && '注文中', on('発送') && '発送済み']
    .filter(Boolean);
  const warnLine = busy.length
    ? `\n\n※この看板はいま「${busy.join('・')}」です。消すと倉庫からも見えなくなります。`
    : '';
  if (!window.confirm(`${v.table} から ${what} を消します。\n元に戻せません。${warnLine}`)) return;

  const creds = await askPassword(`${v.table} から ${what} を消します。`);
  if (!creds) return;

  try {
    const r = await api.post('/api/master/delete',
      { table: v.table, row_key: row[v.row_key], key: row[v.key_column], db: v.db || '', ...creds });
    ok(r.message || '消しました');
    showMasterWritten(r);
    openTable(v.page);
  } catch (e) {
    bad(e.message);
    if (e.status === 409) openTable(v.page);   // 他端末が先に消していた
  }
}
