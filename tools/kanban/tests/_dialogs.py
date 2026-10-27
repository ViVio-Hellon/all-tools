"""画面の中の確かめの窓(``app/static/js/dialog.js``)に、試験が答える。

看板はブラウザ標準の確かめの窓(confirm・alert)を使わない ── 出しているあいだ
画面のタイマーが止まり、心拍が途切れてサーバが終わってしまうため。代わりに画面の
中に窓を出す。Playwright の ``page.on("dialog")`` はこの窓を見ないので、ここで
同じ役をする:

    - 窓が出たら、その文言を ``decide(文言)`` に渡す
    - ``True`` なら決めるボタン、``False`` ならやめるボタンを押す
    - ``None`` なら**答えずに置く**(出したまま手を止めた人の代わり)

答えは Python が決める(``expose_binding``)。Playwright の呼び出しを待っている
あいだに捌かれる(``page.on("dialog")`` と同じ)ので、試験は今までどおり
``wait_for_*``・``wait_until`` で待てばよい。

**標準の窓が出たら試験を落とす**(``native_guard``)。戻すと心拍が止まる不具合が
戻るので、どの画面の試験でも気づけるようにする。
"""
from __future__ import annotations

from typing import Callable, Optional

#: 窓が出たのを見つけて Python に訊き、答えのボタンを押す(どのページ・どの枠にも入れる)
ANSWER_JS = """
(() => {
  if (window.__dialogWatch) return;
  window.__dialogWatch = true;
  const seen = new WeakSet();
  const look = () => {
    for (const box of document.querySelectorAll('.ask__box')) {
      if (seen.has(box)) continue;
      seen.add(box);
      const msg = box.querySelector('.ask__msg');
      const text = msg ? msg.textContent : '';
      if (typeof window.__answerDialog !== 'function') continue;
      Promise.resolve(window.__answerDialog(text)).then((yes) => {
        if (yes === null || yes === undefined || !box.isConnected) return;
        const pick = box.querySelector(yes ? '[data-ask="yes"]' : '[data-ask="no"]')
          || box.querySelector('[data-ask="yes"]');
        if (pick) pick.click();
      }).catch(() => {});
    }
  };
  new MutationObserver(look).observe(document, { childList: true, subtree: true });
})();
"""


def answer_dialogs(target, decide: Callable[[str], Optional[bool]]) -> None:
    """``target``(ページか文脈)に出る確かめの窓へ ``decide`` で答える。

    **開く前に呼ぶ**(文脈か、``goto`` する前のページ)。初期化の台本は次に読み込む
    ページから効く。
    """
    target.expose_binding("__answerDialog", lambda _source, text: decide(text))
    target.add_init_script(ANSWER_JS)


def native_guard(page, found: list) -> None:
    """ブラウザ標準の確かめの窓が出たら ``found`` に残して閉じる(試験の側で落とす)。"""
    def seen(dialog) -> None:
        try:
            if dialog.type == "beforeunload":
                # 「このページを離れますか？」はブラウザが出すもので、画面の中には出せない。
                # 今までどおり離れる
                dialog.accept()
                return
            found.append(f"{dialog.type}: {dialog.message}")
            dialog.dismiss()
        except Exception:                       # noqa: BLE001 - もう閉じた画面
            pass
    page.on("dialog", seen)
