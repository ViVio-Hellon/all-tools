/*
  guide_notice.js — 案内表示(v4.47.0)

  1分ごとの見張り(`shift_end.js` の tick)で `/api/notice/due` を聞き、案内があれば出す。
  **出すかどうか・何を出すかはサーバが決める**(`logic/guide_notice.py`)── ここは写すだけ。

    大 … 画面いっぱい(既定)/ 中 … 真ん中の窓 / 小 … 上の帯
    **押すまで閉じない**(×も Esc も無い)。名前を選ぶ案内は、名前を選ぶまで押せない。
    押したら `/api/notice/ack`。サーバがその直・その案内を覚えるので、次からは出ない。

  名前は班員名簿(`/api/staff/members`)から選ぶ。その直の作業者がもう選ばれていれば、
  最初からその人を選んでおく。名簿に無い人は下の欄に打てる。
*/
import { background } from "./api.js";
import { toast } from "./toast.js";

const BOX_ID = "guide-notice";
let shown = null;          // いま出している案内 {id, report_date, shift}
let roster = null;         // 班員名簿(1度だけ読む)

async function loadRoster() {
  if (roster) return roster;
  try {
    const body = await background.get("/api/staff/members");
    roster = body.teams || [];
  } catch (err) {
    roster = [];
  }
  return roster;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function close() {
  document.getElementById(BOX_ID)?.remove();
  shown = null;
}

async function paint(body) {
  const notice = body.notice;
  const box = el("div", `gnote gnote--${{ 大: "large", 中: "medium", 小: "small" }[notice.size] || "large"}`
                 + ` gnote--${{ 注意: "caution", 警告: "warning", 案内: "info" }[notice.color] || "caution"}`);
  box.id = BOX_ID;
  box.setAttribute("role", "alertdialog");
  box.setAttribute("aria-modal", "true");
  box.dataset.notice = notice.id;
  const panel = el("div", "gnote__panel");
  panel.append(el("p", "gnote__text", notice.text));
  if (notice.question) panel.append(el("p", "gnote__question", notice.question));

  let chosen = body.worker || "";
  const button = el("button", "btn btn--primary gnote__ok", notice.button);
  button.type = "button";
  const note = el("p", "gnote__note", "");
  const refresh = () => {
    button.disabled = notice.pick_worker && !chosen;
    note.textContent = notice.pick_worker
      ? (chosen ? `押す人: ${chosen}` : "押す人の名前を選んでください")
      : "";
  };

  if (notice.pick_worker) {
    const pick = el("div", "gnote__pick");
    pick.append(el("p", "gnote__label", "押す人"));
    const names = el("div", "gnote__names");
    const all = [];
    for (const team of await loadRoster()) {
      for (const name of team.names || []) {
        const b = el("button", "btn gnote__name", name);
        b.type = "button";
        b.dataset.name = name;
        b.setAttribute("aria-pressed", String(name === chosen));
        b.addEventListener("click", () => {
          chosen = name;
          typed.value = "";
          for (const x of all) x.setAttribute("aria-pressed", String(x.dataset.name === chosen));
          refresh();
        });
        all.push(b);
        names.append(b);
      }
    }
    const typed = el("input", "gnote__typed");
    typed.type = "text";
    typed.placeholder = all.length ? "名簿に無いときはここに名前" : "名前を入れてください";
    typed.setAttribute("aria-label", "押す人の名前");
    if (chosen && !all.some((b) => b.dataset.name === chosen)) typed.value = chosen;
    typed.addEventListener("input", () => {
      chosen = typed.value.trim();
      for (const x of all) x.setAttribute("aria-pressed", String(x.dataset.name === chosen));
      refresh();
    });
    pick.append(names, typed);
    panel.append(pick);
  }
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    try {
      const res = await background.post("/api/notice/ack", {
        id: notice.id, worker: chosen, report_date: body.report_date, shift: body.shift });
      close();
      toast(res.message || "記録しました", "ok");
      check();                                  // 同じ直にほかの案内があれば続けて
    } catch (err) {
      note.textContent = err.message || "記録できませんでした。もう一度押してください";
      if (err.status === 409 || err.status === 404) { close(); check(); return; }
      refresh();
    }
  });
  panel.append(note, button);
  box.append(panel);
  refresh();
  // **押すまで閉じない。** Esc で閉じる画面の決まりに乗らないよう、ここで止める
  box.addEventListener("keydown", (e) => { if (e.key === "Escape") e.stopPropagation(); });
  document.body.append(box);
  (notice.pick_worker && !chosen ? box.querySelector(".gnote__name, .gnote__typed") : button)?.focus();
}

/** 見張りから呼ぶ。出すものが変わったときだけ描き直す(選んでいる名前を消さない)。 */
export async function check() {
  let body;
  try {
    body = await background.get("/api/notice/due");
  } catch (err) {
    return;                                     // 繋がらないのは health.js が知らせる
  }
  const notice = body && body.notice;
  const key = notice ? `${notice.id}\t${body.report_date}\t${body.shift}` : "";
  if (shown === key) return;
  close();
  if (!notice) return;
  shown = key;
  await paint(body);
}
