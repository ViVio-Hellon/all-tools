/*
  line_label.js — ラインを画面に出す字(v4.12.5 / v4.13.0)

  v4.13.0 からツールの名前が正規の呼び名(L-1・機側・トット・バランサー・中板…)
  なので、ほぼそのまま出します。することは2つだけ:

      中板 + 設備番号 → 中板4
      v4.12 までの名前(LS・MARU …)が来たら正規の呼び名で出す

  表はサーバが持っています(`logic/line_names.labels()`)。`base.html` が
  `<body data-line-labels>` に置いたものを引くだけ。知らない値はそのまま返します。
*/

let table = null;

function load() {
  if (table) return table;
  try {
    table = JSON.parse(document.body?.dataset.lineLabels || "{}");
  } catch (e) {
    table = {};
  }
  return table;
}

/** 名前 → 見せる字。中板は設備番号があれば「中板4」。 */
export function lineLabel(code, number = "") {
  const text = code == null ? "" : String(code);
  const found = load()[text];
  if (!found) return text;
  return found === "中板" && number ? `${found}${number}` : found;
}
