// 外枠の配線。どの画面でも動く
//
// 画面ごとの処理は `views/*.js` にあり、テンプレートの `{% block scripts %}`
// から読み込まれる。ここは外枠(帯・終了ボタン・接続監視)だけを持つ。

import { api, SCREEN } from './api.js';
import * as desktop from './desktop.js';
import { startHealthWatch } from './health.js';
import { quietReload } from './leave.js';
import { wireThemeToggle } from './theme.js';
import { bad, ok } from './toast.js';

// デスクトップ版(Rust/Tauri の窓)なら、帳票の窓・保存ダイアログを外枠に頼む
desktop.install();
wireThemeToggle();
startHealthWatch();
startHeartbeat();
wireQuit();
reportClientErrors();

/**
 * 画面(ブラウザ)の中で起きたエラーを記録に送る(kanban/trace.py の「画面のエラー」)。
 *
 * **画面の不具合はサーバのログに出ない。** 押しても何も起きない・表示が途中で止まった、
 * はたいていここで起きている。同じものを続けて送らない(10 秒に 1 回まで)。送れなくても
 * 何もしない(記録のために画面を止めない)。
 */
function reportClientErrors() {
  const sent = new Map();
  const send = (info) => {
    try {
      const key = `${info.message}|${info.source}|${info.line}`;
      const now = Date.now();
      if (sent.has(key) && now - sent.get(key) < 10000) return;
      sent.set(key, now);
      api.post('/api/client-log', { ...info, page: location.pathname }).catch(() => {});
    } catch (e) { /* 送れなくてよい */ }
  };
  window.addEventListener('error', (ev) => {
    if (!ev || !ev.message) return;   // 画像の読み込み失敗などは送らない
    send({
      type: (ev.error && ev.error.name) || 'Error', message: String(ev.message),
      source: ev.filename || '', line: ev.lineno || '', col: ev.colno || '',
      stack: (ev.error && ev.error.stack) || '',
    });
  });
  window.addEventListener('unhandledrejection', (ev) => {
    const r = ev && ev.reason;
    // サーバが断った・繋がらないは、サーバ側の記録に残っている(ここで二重にしない)
    if (r && r.name === 'ApiError') return;
    send({
      type: (r && r.name) || 'UnhandledRejection', message: String((r && r.message) || r),
      source: '', line: '', col: '', stack: (r && r.stack) || '',
    });
  });
}

/**
 * 心拍 (基盤仕様書 2.8「自動終了」)
 *
 * **このアプリに窓は無い。** 見えているのはブラウザのタブだけなので、
 * タブを閉じたら終わったつもりになる。ところが `pythonw.exe` は動いた
 * ままで、しかもウィンドウを持たないのでタスクマネージャーの「アプリ」
 * にも出てこない。次に起動すると「すでに起動しています」と言われ、
 * **閉じたのに開けない**状態になる(実際にそうなった)。
 *
 * ここが定期的に「生きている」と伝え、閉じるときは `sendBeacon` で
 * 即座に伝える。判断はサーバ側(`kanban/idle_exit.py`)が持つ。
 *
 * **裏に回ったときも伝える。** ブラウザは裏のタブのタイマーを間引く
 * (Chrome は 5 分隠れると 1 分に 1 回)・止める(Edge のスリープ中のタブ)
 * ので、心拍が途切れても閉じたとは限らない。別の道具ではこれで、開いて
 * いるのに終了した。裏に回る時点で「隠れます」と送っておけば、サーバは
 * 心拍が途切れても待つ(`kanban/screen.py` の `HIDDEN_MAX_SEC`)。
 *
 * **戻ってきたことにも気付く。** 表に戻った・凍結が解けた・PC がスリープ
 * から起きた、のどれでも、待たずに心拍を送り、`app-resumed` を投げて
 * 盤と接続の様子を取り直させる(止まっていたあいだの変化を見せたままにしない)。
 */

/** ブラウザが捨てて読み込み直したタブの、前の名札を覚えておく場所 */
const SCREEN_KEY = 'kanban.screen';

function startHeartbeat() {
  const ms = window.APP.alivePollMs || 5000;
  // 見えているのに、次の心拍がこれ以上遅れて回ってきたら「止まっていた」
  // (PC のスリープ・休止)と読む。見えているタブのタイマーは間引かれない
  const SLEEP_GAP_MS = Math.max(30000, ms * 4);

  // **捨てられて読み込み直したタブは、前の名札を引き取る。**
  // ブラウザ(Chrome のメモリセーバーなど)が裏のタブを捨てると、閉じた合図は
  // 飛ばない。前の名札が「隠れた画面」として残り、読み込み直した自分自身を
  // 「別のタブ」と数えてしまう。名札そのものは毎回作り直す(api.js)ので、
  // 引き取るのは `document.wasDiscarded` のときだけ ── タブの複製では引き取らない
  let replaces = '';
  try {
    if (document.wasDiscarded) replaces = sessionStorage.getItem(SCREEN_KEY) || '';
    sessionStorage.setItem(SCREEN_KEY, SCREEN);
  } catch (e) { /* 使えなくても心拍は送れる */ }

  // デスクトップ版は名乗る(外枠の機能が画面に届いているかを Python のログに残す)
  const kind = desktop.isDesktop ? '&desktop=1' : '';
  const url = (extra) => `/api/alive?screen=${encodeURIComponent(SCREEN)}${kind}${extra}`;

  // **デスクトップ版は、開いたページが持ち主を引き継ぐ。** 業務の画面を出す窓は
  // 1 つだけ(帳票の窓は持ち主を名乗らない)なので、取り合う相手は同じ窓の前の
  // ページしか居ない。しかも窓の中では離脱の合図(`sendBeacon`)が届かず、前の
  // ページが持ち主のまま残る ── 盤から設定へ移ると設定の操作が断られた
  let takeOver = desktop.isDesktop;

  const beat = async () => {
    // 裏で(間引かれながら)届く心拍にも「隠れています」を付ける。
    // 裏に回るときの合図が届かなかった場合も、これで伝わる
    let extra = document.hidden ? '&hidden=1' : '';
    if (replaces) extra += `&replaces=${encodeURIComponent(replaces)}`;
    if (takeOver) extra += '&take=1';
    try {
      // **トークンを付けない。** この経路は素通しで、返すのは「受け取った」
      // だけ。トークンが切れた画面が黙って死んだ扱いになるのを避ける
      const res = await fetch(url(extra), { method: 'POST', cache: 'no-store' });
      replaces = '';
      takeOver = false;
      const body = await res.json();
      // `active` を返さないサーバ(古い版)では覆いを出さない
      if ('active' in body) showHolder(body.active);
    } catch (e) { /* 接続断は health.js が帯で知らせる */ }
  };

  // **離脱中でも届く合図**(fetch は捨てられるが `sendBeacon` は届く)
  const signal = (what) => {
    try {
      navigator.sendBeacon(url(`&${what}=1`), new Blob([], { type: 'text/plain' }));
    } catch (e) { /* 対応していないブラウザは心拍だけで判断される */ }
  };

  let lastTick = Date.now();
  let hiddenAt = document.hidden ? Date.now() : null;

  // 戻ってきた。待たずに心拍を送り、画面に取り直させる
  const resumed = (reason, gapMs = 0) => {
    lastTick = Date.now();
    beat();
    window.dispatchEvent(new CustomEvent('app-resumed', { detail: { reason, gapMs } }));
  };

  beat();
  setInterval(() => {
    const now = Date.now();
    const gap = now - lastTick;
    lastTick = now;
    if (!document.hidden && gap > SLEEP_GAP_MS) {
      // 見えているのに大きく遅れた = PC が止まっていた(スリープ・休止)
      resumed('sleep', gap);
      return;
    }
    beat();
  }, ms);

  // 裏に回る / 表に戻る
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      hiddenAt = Date.now();
      signal('hidden');
    } else {
      resumed('visible', hiddenAt ? Date.now() - hiddenAt : 0);
      hiddenAt = null;
    }
  });
  // 凍結される / 凍結が解ける(Page Lifecycle。Edge のスリープ中のタブなど)
  document.addEventListener('freeze', () => signal('hidden'));
  document.addEventListener('resume', () => resumed('resume'));
  // 戻る・進むのキャッシュから戻った(ページはそのまま、タイマーは止まっていた)
  window.addEventListener('pageshow', (ev) => { if (ev.persisted) resumed('restore'); });
  // ネットワークが戻った(スリープ明けに多い)
  window.addEventListener('online', () => resumed('online'));

  // 閉じた合図。再読込でも飛ぶが、サーバは猶予を置いてから落とすので取り消せる。
  //
  // **名札を必ず添える。** これが無いと、2枚開いているうちの1枚を閉じた
  // だけでアプリごと終わる(実際にそうなっていた)
  //
  // **戻る・進むのキャッシュへ入るときは「閉じた」ではない**(`persisted`)。
  // 閉じたと伝えると、戻ってくる前に終わってしまう。隠れた扱いにしておく。
  // `beforeunload` では送らない ── キャッシュへ入るかどうかがまだ分からない
  window.addEventListener('pagehide', (ev) => signal(ev.persisted ? 'hidden' : 'leaving'));
}

/**
 * 「この画面は使えません」の覆い (`kanban/screen.py`)
 *
 * **2枚開けること自体は止められない。** ショートカットをもう一度押せば
 * タブは増えるし、アドレスを控えて開くこともできる。止められるのは
 * **2枚とも押せてしまうこと**のほうで、それをここで出す。
 *
 * 隠したり閉じさせたりはしない。開いてしまったものを黙って無効にすると
 * 「押しても何も起きない」に見える ── **なぜ使えないのかと、どうすれば
 * 使えるか**を出すほうが早く終わる。
 */
let holding = true;

// 操作が 423 で断られた = 持ち主ではない。心拍を待たずに覆いを出す
window.addEventListener('screen-blocked', () => showHolder(false));

function showHolder(active) {
  if (active === holding) return;
  holding = active;
  if (active) {
    // 持ち主に戻った。**盤を取り直す。** 使えなかったあいだに他の端末が
    // 動かしているので、そのまま操作させると古い盤を押すことになる。
    // 書きかけのコメントは置いてから読み直す(leave.js)。置けなければ読み直さず、
    // 覆いだけ外して盤を取り直す
    if (!quietReload()) {
      const cover = document.getElementById('screen-blocked');
      if (cover) cover.remove();
    }
    return;
  }
  document.body.appendChild(blockedOverlay());
}

function blockedOverlay() {
  const el = document.createElement('div');
  el.id = 'screen-blocked';
  el.setAttribute('role', 'alertdialog');
  el.setAttribute('aria-modal', 'true');
  el.innerHTML = `
    <div class="blocked__card">
      <p class="blocked__title">この画面では操作できません</p>
      <p class="blocked__body">
        このアプリは<strong>別のタブ</strong>で開いています。<br>
        2枚とも押せる状態にしておくと、同じ看板を2画面で取り合うことになり、
        片方が古い盤のまま押してしまいます。
      </p>
      <button type="button" class="btn btn--primary" id="screen-take">こちらの画面を使う</button>
      <p class="blocked__note">
        押すと、もう一方のタブが使えなくなります。<br>
        もう一方のタブを使うなら、<strong>このタブは閉じてかまいません。</strong>
      </p>
    </div>`;

  const btn = el.querySelector('#screen-take');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await fetch(`/api/alive?take=1&screen=${encodeURIComponent(SCREEN)}`,
                  { method: 'POST', cache: 'no-store' });
      // 取り直してから使う。使えなかったあいだの変化を持ち込まない
      // (書きかけのコメントは置いてから。leave.js)
      holding = true;
      if (!quietReload()) el.remove();
    } catch (e) {
      btn.disabled = false;
      bad('切り替えられませんでした');
    }
  });
  // 覆いが出た時点でここしか押せない。探させない
  setTimeout(() => btn.focus(), 0);
  return el;
}

/**
 * 「終了」ボタン (基盤仕様書 2.8)
 *
 * **タブを閉じるのとは違う。** タブを閉じてもバックエンドは残るので、
 * 明示的に止める道を用意する。取り込み・書き戻しの途中なら、サーバが
 * 409 で断って理由を返す ── そのときだけ「それでも終了する」を訊く。
 */
function wireQuit() {
  const btn = document.getElementById('quit');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await api.post('/api/shutdown', { force: false });
      showStopped();
    } catch (e) {
      if (e.status === 409) {
        // 処理中。何が動いているかを見せてから訊く
        const reason = (e.body && e.body.busy) || e.message;
        if (confirm(`${reason}\n\nそれでも終了しますか？`)) {
          try {
            await api.post('/api/shutdown', { force: true });
            showStopped();
            return;
          } catch (e2) { bad('終了できませんでした'); }
        }
      } else if (e.status === 0) {
        // 届かなかった = もう止まっている
        showStopped();
        return;
      } else {
        bad(e.message);
      }
      btn.disabled = false;
    }
  });
}

function showStopped() {
  document.body.innerHTML =
    '<div style="min-height:100vh;display:grid;place-items:center;' +
    'font-family:var(--font,sans-serif);color:var(--muted)">' +
    '<div style="text-align:center">' +
    '<p style="font-size:1.05rem;font-weight:700;color:var(--fg)">終了しました</p>' +
    '<p>このタブは閉じてかまいません。</p></div></div>';
}
