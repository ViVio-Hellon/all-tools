// バックエンドの生存監視 (基盤仕様書 2.9)
//
// **ブラウザが開いていることと、バックエンドが動いていることは別。**
// Python 側が落ちても、画面はそのまま古い盤面を出し続ける。看板は
// 「いまの状態」を見るための画面なので、それは事故そのもの。
// 接続が切れたら帯を出し、再接続の道を示す。

import { api, onConnectionChange } from './api.js';

const POLL_MS = window.APP.healthPollMs || 15000;

let banner = null;
let offline = false;

export function startHealthWatch() {
  banner = document.getElementById('offline');

  const reconnect = document.getElementById('reconnect');
  if (reconnect) reconnect.addEventListener('click', () => check(true));

  // api.js が「届かなかった」と分かった時点で即座に帯を出す。
  // 次の周期(最大15秒)を待たない
  onConnectionChange((up) => setOffline(!up));

  check();
  setInterval(check, POLL_MS);

  // タブへ戻ってきた瞬間に確かめる。伏せているあいだにサーバが
  // 止まっていることがある
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) check();
  });
  // スリープ明け・凍結が解けたとき(app.js が気付いて知らせる)
  window.addEventListener('app-resumed', () => check());
}

async function check(manual = false) {
  try {
    const h = await api.get('/api/health');
    setOffline(false);
    if (manual) location.reload();
    return h;
  } catch (e) {
    setOffline(true);
    return null;
  }
}

function setOffline(state) {
  if (state === offline) return;
  offline = state;
  if (banner) banner.hidden = !state;
}

export function isOffline() { return offline; }
