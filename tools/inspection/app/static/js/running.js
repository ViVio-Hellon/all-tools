/*
  running.js — 帯の「いま動いているもの」(python-web-tools と同じ見せ方)

  印刷・プレビューの作成・フォルダ検索のどれかが動いているあいだだけ出る。
  中身はサーバの答え(`/api/health` の `job`)か、印刷の見張りが入れる。
*/

let local = null;      // 印刷の見張りが直接入れたもの(こちらが新しい)
let fromServer = null;

function paint() {
  const box = document.getElementById("rb-running");
  if (!box) return;
  const job = local || fromServer;
  box.hidden = !job;
  if (!job) return;
  document.getElementById("rb-run-what").textContent = job.label || "";
  document.getElementById("rb-run-how").textContent = job.message || "";
  const pct = job.total ? Math.round((Math.max(0, job.current) / job.total) * 100) : 0;
  const fill = document.getElementById("rb-run-fill");
  fill.style.width = `${job.total ? pct : 100}%`;
  box.title = `${job.label} — ${job.message || ""}`;
}

/** サーバの生存確認の答えから写す。 */
export function fromHealth(body) {
  fromServer = (body && body.job) || null;
  paint();
}

/** 画面の中で分かったこと(印刷の進み具合)を写す。`null` で消す。 */
export function set(job) {
  local = job;
  paint();
}
