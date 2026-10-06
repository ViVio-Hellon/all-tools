/*
  dialog.js — 確認のモーダル

  **止める確認は取り消せない操作だけに使う**(印刷・終了)。
  それ以外はトースト(toast.js)で知らせ、作業を止めない。
*/

const $ = (id) => document.getElementById(id);

/**
 * @param {{title?:string, message:string, okText?:string, cancelText?:string, danger?:boolean}} o
 * @returns {Promise<boolean>}
 */
export function confirmDialog(o) {
  const dlg = $("confirm-dialog");
  const yes = $("confirm-yes");
  const no = $("confirm-no");
  $("confirm-title").textContent = o.title || "確認";
  $("confirm-body").textContent = o.message || "";
  yes.textContent = o.okText || "OK";
  yes.className = `btn ${o.danger ? "btn--commit" : "btn--print"}`;
  no.textContent = o.cancelText || "キャンセル";
  return new Promise((resolve) => {
    const done = (value) => {
      yes.removeEventListener("click", onYes);
      no.removeEventListener("click", onNo);
      dlg.removeEventListener("cancel", onCancel);
      if (dlg.open) dlg.close();
      resolve(value);
    };
    const onYes = () => done(true);
    const onNo = () => done(false);
    const onCancel = (ev) => { ev.preventDefault(); done(false); };
    yes.addEventListener("click", onYes);
    no.addEventListener("click", onNo);
    dlg.addEventListener("cancel", onCancel);
    dlg.showModal();
    yes.focus();
  });
}
