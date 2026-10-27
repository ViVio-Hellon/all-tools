/*
  other_terminal.js — 「この直は ほかの端末でも入力されています」の帯(v4.44.10)

  同じラインの2台目の端末で打つと、あとから保存したほうの中身で前の行が置き換わります。
  管理者が入力中の直を呼び出して直しても、ラインの端末が次に保存すると直した中身が
  置き換わります。**止めはしません(知らせだけ)。**

  出すかどうか・文言は**サーバが決めます**(`services/other_terminal`)。ここは写すだけ。
  塗るのは入力画面の応答(`views/entry.js` の `paint`)と、1分ごとの見張り
  (`shift_end.js` の `/api/shift/key`)の2か所 ── 打っていない画面にも、保存する前に出すため。

  帯は消えません(直るまで出し続ける)。**新しく出たとき・中身が変わったときだけ**トーストも
  出します(同じ知らせで何度も驚かせない)。
*/
import { toast } from "./toast.js";

let shown = "";

/** 帯を塗る。`notice` が無い応答(付いていない道)では触らない。 */
export function paintOtherTerminal(notice) {
  const box = document.getElementById("other-terminal");
  if (!box || !notice) return;
  const text = notice.shown ? notice.text || "" : "";
  box.hidden = !text;
  box.textContent = text;
  box.dataset.kind = notice.shown ? notice.kind || "" : "";
  if (text && text !== shown) toast(text, "warn");
  shown = text;
}
