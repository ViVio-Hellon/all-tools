/*
  test_band.js — テストモードの帯(入った・抜けたで描き直す)と「テストモードを抜ける」

  帯は外枠の側(`base.html`)にあり、`nav.js` の差し替えでは変わりません。入る・抜けるのあとは
  ここで帯を描き直し、画面の中身は差し替えで取り直します(`nav.refresh()` ── 画面ごと読み直すと
  出したばかりのトーストや接続断の帯が消えるので)。判断はサーバ(`nippou/test_mode`)。
*/
import { api } from "./api.js";
import { refresh } from "./nav.js";
import { toastError } from "./toast.js";

function bandOf(state) {
  const band = document.createElement("div");
  band.className = "testband";
  band.id = "test-band";
  band.setAttribute("role", "status");
  band.dataset.active = "1";
  const mark = document.createElement("span");
  mark.className = "testband__mark";
  mark.setAttribute("aria-hidden", "true");
  mark.textContent = "TEST";
  const text = document.createElement("span");
  text.className = "testband__text";
  text.id = "test-band-text";
  text.textContent = state.band || "";
  band.append(mark, text);
  if (state.since_text) {
    const since = document.createElement("small");
    since.className = "testband__note";
    since.textContent = `${state.since_text} から`;
    band.append(since);
  }
  const leave = document.createElement("button");
  leave.className = "btn btn--sm";
  leave.type = "button";
  leave.id = "test-band-leave";
  leave.textContent = "テストモードを抜ける";
  band.append(leave);
  return band;
}

/** いまのテストモードで帯を描き直す(入っていれば出す・抜けていれば消す)。 */
export async function paintBand() {
  const state = await api.get("/api/test-mode");
  const old = document.getElementById("test-band");
  if (state.active) {
    const band = bandOf(state);
    if (old) old.replaceWith(band);
    else document.body.insertBefore(band, document.querySelector("header.ribbon") || document.body.firstChild);
  } else if (old) {
    old.remove();
  }
  return state;
}

/** 入る(管理者パスワード)。帯を描き直して画面の中身を取り直す。 */
export async function enterTestMode(password) {
  const body = await api.post("/api/test-mode", { enable: true, password });
  await paintBand();
  await refresh();
  return body;
}

/** 抜ける。 */
export async function leaveTestMode() {
  const body = await api.post("/api/test-mode", { enable: false });
  await paintBand();
  await refresh();
  return body;
}

// 帯の釦は描き直すたびに作り直すので、外枠に1つだけ付ける
document.addEventListener("click", async (event) => {
  if (!event.target.closest?.("#test-band-leave")) return;
  try {
    await leaveTestMode();
  } catch (err) {
    toastError(err);
  }
});
