/*
  adminpass.js — 管理者パスワードを聞いて、同じ要求をもう一度送る

  **押す前から欄を出しません。** 要るかどうかはサーバが持っている
  (同じ値で保存し直すだけなら聞かない)ので、画面が先回りして判断すると
  必ず食い違います。断られてから聞いて、送り直します。

  **同じ問いを黙って出し直しません。** 打ち間違えたときは、聞き直す前に
  「違います」と言う ── 言わないと、間違えているのか壊れているのかが
  利用者には分からず、終わりの無い問答になります(実際になりました)。

  設定画面(ライン・参照パス)とマスタ確認(端末のライン予約)の両方が
  同じ関門を通るので、**ここ1つに置いてあります。**
*/

import { ApiError } from "./api.js";
import { modal } from "./modal.js";

/** 続けて聞く上限。これを超えたら**やめて、先へ進む道を出す**。 */
export const TRIES = 3;

/** サーバが「パスワードが要る/違う」と言ってきたか。 */
export function asksPassword(err) {
  return err instanceof ApiError
    && (err.code === "need_password" || err.code === "wrong_password");
}

/**
 * 管理者パスワードを聞く。取り消しなら `null`。
 *
 * @param {string} why   サーバが言ってきたこと(違うならその旨も入っている)
 * @param {boolean} wrong 直前に打った値が違ったか
 */
export function ask(why, wrong = false) {
  return modal({
    title: wrong ? "管理者パスワードが違います" : "管理者パスワード",
    render(body) {
      const p = document.createElement("p");
      p.style.whiteSpace = "pre-wrap";
      p.style.lineHeight = "1.7";
      p.textContent = why;
      const field = document.createElement("div");
      field.className = "field";
      const input = document.createElement("input");
      input.className = "input";
      input.type = "password";
      input.id = "admin-pass";
      input.setAttribute("data-autofocus", "1");
      field.appendChild(input);
      body.append(p, field);
    },
    actions: [
      { label: "取り消し", value: null },
      { label: "続ける", kind: "primary",
        // 空のまま押しても閉じない(送っても断られるだけ)
        value: () => document.getElementById("admin-pass").value || undefined },
    ],
  });
}

/**
 * パスワードを求められたら聞いて送り直す。**回数を決めて終わる。**
 *
 * @param {(body:object) => Promise<any>} send 中身を受け取って投げる関数
 * @param {object} body 送る中身(`password` はこちらが足す)
 * @returns {Promise<{ok:boolean, payload?:any, error?:any, cancelled?:boolean,
 *                    exhausted?:boolean}>}
 */
export async function withPassword(send, body) {
  let current = body;
  for (let tries = 0; tries <= TRIES; tries += 1) {
    try {
      return { ok: true, payload: await send(current) };
    } catch (err) {
      if (!asksPassword(err)) return { ok: false, error: err };
      if (tries >= TRIES) {
        // ここまで来たら聞き直さない。**行き止まりにしない**ために、
        // 最後にサーバが言ったこと(戻し方を含む)をそのまま渡す
        return { ok: false, error: err, exhausted: true };
      }
      const password = await ask(err.message, err.code === "wrong_password");
      // **打った値は持ち回さない。** この1回の送信にだけ使う
      if (password === null) return { ok: false, cancelled: true };
      current = { ...current, password };
    }
  }
  return { ok: false, cancelled: true };
}
