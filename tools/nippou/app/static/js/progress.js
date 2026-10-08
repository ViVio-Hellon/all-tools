/*
  progress.js — 時間のかかる仕事の進み具合を、画面の上に出す

      保存処理にもプログレスを表示し進捗がわかるようにしてください

  共有へ保存・マスタの1行を直す、はどちらも**終わるまで返りません。**
  共有のフォルダが遠い日は何秒もかかり、そのあいだ画面はボタンがくるくる
  回るだけでした ── 何をしていて、あとどれくらいかが分からないと、人は
  もう一度押すか、閉じます。

  **数と言葉を持つのはサーバ**(`nippou/job_progress.py` と
  `logic/progress.py`)。ここは 0.4 秒ごとに `GET /api/progress` を見に行き、
  返ってきた字をそのまま出すだけです。

  枠は画面の上の真ん中に浮かせます(画面ごとに置き場所を作らない ──
  日報入力でも設定でも、マスタの窓の上でも同じ所に出ます)。
  **0.3 秒より早く終わるものには出しません**(ちらつくだけなので)。

      const stop = watch("共有へ保存しています");
      try { await api.post(...); } finally { stop(); }
*/
import { api, background } from "./api.js";

const POLL_MS = 400;
const SHOW_AFTER_MS = 300;

function panel() {
  let box = document.getElementById("job-float");
  if (box) return box;
  box = document.createElement("div");
  box.id = "job-float";
  box.className = "job job--float";
  box.hidden = true;
  box.setAttribute("role", "status");
  box.setAttribute("aria-live", "polite");
  box.innerHTML = '<div class="job__title"></div>'
    + '<div class="job__head"></div>'
    + '<progress class="job__bar" max="100"></progress>'
    + '<div class="lead job__note"></div>'
    + '<div class="job__acts"><button class="btn btn--sm job__stop" type="button" hidden>'
    + '中止(いまの束を送ったら止める)</button></div>';
  document.body.append(box);
  // **共有へ保存は途中で止められる。** 送った分は共有に入り、残りは次に押せば続きから
  box.querySelector(".job__stop").addEventListener("click", async (ev) => {
    ev.currentTarget.disabled = true;
    try { await api.post("/api/progress/stop", {}); } catch { /* もう終わっている */ }
  });
  return box;
}

function paint(box, now, title) {
  box.querySelector(".job__title").textContent = now.title || title || "";
  box.querySelector(".job__head").textContent = now.headline || title || "";
  box.querySelector(".job__note").textContent = now.note || "";
  const stopBtn = box.querySelector(".job__stop");
  stopBtn.hidden = !now.can_stop;
  if (now.can_stop) stopBtn.disabled = false;
  const bar = box.querySelector(".job__bar");
  // **総数が分からないうちは、止まった棒を出さない**(動いている棒にする)
  if (now.total > 0) bar.value = now.percent;
  else bar.removeAttribute("value");
}

let active = 0;       // 同時に見ている数(入れ子になっても最後の1つで消す)

/**
 * 進み具合を見はじめる。返り値を呼ぶと止めて枠を消す。**必ず呼ぶ**
 * (`finally`)── 呼ばないと枠が出たままになります。
 */
export function watch(title = "") {
  const box = panel();
  active += 1;
  let stopped = false;
  let timer = 0;
  const show = setTimeout(() => {
    if (stopped) return;
    paint(box, { headline: title, total: 0 }, title);
    box.hidden = false;
    timer = setInterval(async () => {
      try {
        // 誰も押していない通信。**待機の姿を出さない**(`background`)
        const now = await background.get("/api/progress");
        if (!stopped && now && now.running) paint(box, now, title);
      } catch { /* 読めなくても仕事は進んでいます */ }
    }, POLL_MS);
  }, SHOW_AFTER_MS);

  return () => {
    if (stopped) return;
    stopped = true;
    clearTimeout(show);
    clearInterval(timer);
    active = Math.max(0, active - 1);
    if (!active) box.hidden = true;
  };
}

/**
 * 共有へ保存の前に、**送る数が多ければ確かめる**(押すボタンはどこでもここを通る)。
 *
 *     数が多い場合は分割するか処理前に確認を取ってください
 *
 * 取り込んだ過去の日報がまとめて未送信になっていると、押した瞬間に
 * 何百ページも送り始めていました。数・期間・目安の時間を出して訊きます。
 * 下見が読めなくても押せるようにします(送るのはサーバが決める)。
 */
export async function confirmPush() {
  let plan;
  try { plan = await background.get("/api/settings/push/plan"); } catch { return true; }
  if (!plan || !plan.confirm_needed) return true;
  const span = plan.first_day && plan.last_day && plan.first_day !== plan.last_day
    ? `${plan.first_day} 〜 ${plan.last_day}` : (plan.first_day || "");
  return confirm(
    `共有へまだ送っていない日報が ${plan.pages}ページ(${plan.shifts}直)あります。\n`
    + (span ? `期間: ${span}\n` : "")
    + `全部送るとおよそ ${plan.minutes}分かかります(共有フォルダの速さによります)。\n\n`
    + "25ページずつ区切って送ります。途中で「中止」を押すと、そこまで送った分は"
    + "共有に入り、残りは次に「共有へ保存」を押したときに続きから送ります。\n\n"
    + "送り始めますか?");
}
