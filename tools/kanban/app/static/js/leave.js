// こちらの都合で画面を読み直すとき(覆いが外れた・「こちらの画面を使う」)の約束
//
// **打ちかけを置いてから読み直す。** 読み直すと画面の中にしか無いもの(書きかけの
// コメント)は消える。以前は覆いが外れたとたんに黙って読み直していたので、
//
//     打った行が消えることがありました
//
// ── とんでもない話である。読み直す前に `app:before-reload` を投げ、画面
// (views/board.js)に打ちかけをこの端末へ置いてもらう。置けなかった画面は
// `preventDefault()` で止める。止められたら読み直さず、盤だけ取り直させる
// (`app-resumed`)。
//
// 読み直すのはこちらの都合なので「このページを離れますか？」は出さない
// (打ちかけはもう置いてある)。`isQuietLeave()` が真のあいだは、画面の
// `beforeunload` は訊かない。

let quiet = false;

/** 打ちかけを置いてから読み直す。置けない画面があれば読み直さずに盤だけ取り直す */
export function quietReload() {
  const ev = new CustomEvent('app:before-reload', { cancelable: true });
  if (!window.dispatchEvent(ev)) {
    window.dispatchEvent(new CustomEvent('app-resumed', { detail: { reason: 'reload-held' } }));
    return false;
  }
  quiet = true;
  location.reload();
  return true;
}

/** こちらの都合で離れている最中か(`beforeunload` で訊かない) */
export function isQuietLeave() {
  return quiet;
}
