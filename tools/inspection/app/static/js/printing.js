/*
  printing.js — 印刷と進み具合(VBA版 DoPrint / PrintWorkbooksFromFolder)

  選んだ順に Excel から印刷し、「2 / 5 点検表Bを印刷しています」を出す。
  印刷は裏で進むので、画面を閉じて開き直しても続きから見える。
*/

import { api, screenId } from "./api.js";
import { confirmDialog } from "./dialog.js";
import * as sheet from "./inspection.js";
import * as running from "./running.js";
import { toast, toastError } from "./toast.js";

const $ = (id) => document.getElementById(id);
const MARKS = { ok: "✓", error: "✕", printing: "●", pending: "–", skipped: "–" };
let timer = null;
let lastJob = null;

export async function start() {
  const items = sheet.selectedItems();
  if (!items.length) { toast("印刷する点検表を選んでください。", "warn"); return; }
  const copies = sheet.getCopies();
  if (copies === null) {
    toast(`印刷部数は1～${window.APP.maxCopies || 100}の数値で入力してください。`, "error");
    $("copies").focus();
    return;
  }
  const lines = [`${items.length} 件の点検表を ${copies} 部ずつ印刷します。`, "選んだ順番に印刷します。"];
  if (sheet.state.printer) lines.push("", `プリンター: ${sheet.state.printer}`);
  const ok = await confirmDialog({ title: "印刷の確認", message: lines.join("\n"), okText: "印刷する" });
  if (!ok) return;
  sheet.setPrinting(true);
  try {
    const data = await api.post("/api/print", { ids: items.map((it) => it.id), copies });
    showDialog();
    render(data.job);
    poll();
  } catch (err) {
    if (err.code === "PRINT_RUNNING") {
      // 先に始まっている印刷(ほかの画面のものもある)。理由を出してから進み具合を見せる
      sheet.setPrinting(false);
      toastError(err);
      showDialog();
      poll();
      return;
    }
    sheet.setPrinting(false);
    toastError(err);
  }
}

// 問い合わせの番号。**見張りは1本だけ** ── 印刷を始めた・表に戻った・
// 再接続した、のたびに poll() が呼ばれる。問い合わせの返事を待っているあいだに
// 呼ばれると、返事が来たところでそれぞれが次を仕掛け、見張りが2本・3本と
// 増えていた(古い返事で進み具合が戻って見えることもある)。いちばん新しい
// 1本だけが続きを仕掛ける
let pollSeq = 0;

function poll() {
  clearTimeout(timer);
  const mine = ++pollSeq;
  api.get("/api/print/status").then((data) => {
    if (mine !== pollSeq) return;
    render(data.job);
    if (data.job && data.job.active) timer = setTimeout(poll, window.APP.jobPollMs || 500);
  }, () => {
    if (mine !== pollSeq) return;
    timer = setTimeout(poll, 2000);    // 切れたことは赤い帯が知らせる
  });
}

function showDialog() {
  const dlg = $("print-dialog");
  if (!dlg.open) dlg.showModal();
}

function titleOf(job) {
  if (job.state === "waiting") return "Excel の空き待ち...";
  if (job.state === "running") return "印刷中...";
  if (job.state === "cancelled") return "印刷を中止しました";
  if (job.state === "failed") return "印刷できませんでした";
  return job.failed ? "印刷完了(エラーあり)" : "印刷完了";
}

function render(job) {
  if (!job || job.state === "idle") return;
  lastJob = job;
  const total = job.total || 0;
  const processed = job.succeeded + job.failed;
  const active = !!job.active;
  // ほかの画面(同じPCの別のタブ)から始めた印刷なら、そう書く。自分のものと思って中止しないように
  const other = job.screen_id && job.screen_id !== screenId();
  $("print-title").textContent = titleOf(job) + (other ? "(ほかの画面から開始)" : "");
  $("print-counter").textContent = `${active ? Math.max(job.current_index, 0) : processed} / ${total}`;
  let current = "";
  if (job.state === "waiting") current = "プレビューの作成が終わるのを待っています";
  else if (active) current = job.current_name ? `${job.current_name}を印刷しています` : (job.message || "");
  else if (job.state === "failed") current = job.message || "";
  $("print-current").textContent = current;

  const bar = $("print-bar");
  const ratio = total ? (active ? Math.max(processed, job.current_index - 0.5) : processed) / total : 0;
  bar.style.width = `${Math.round(Math.max(0, Math.min(1, ratio)) * 100)}%`;
  if (active) delete bar.dataset.state;
  else bar.dataset.state = job.failed || job.state === "failed" ? "ng" : "done";

  const summary = $("print-summary");
  delete summary.dataset.state;
  if (!active) {
    // VBA版の結果表示と同じ形
    let text = `印刷完了: ${job.succeeded} ファイル(${job.copies} 部ずつ)`;
    if (job.failed) { text += `\nエラー: ${job.failed} ファイル`; summary.dataset.state = "ng"; }
    if (job.state === "cancelled") text += "\n中止したため印刷していない点検表があります";
    // 失敗があれば問い合わせ番号を添える(設定 →「ログ」で1件ずつの原因を辿れる)
    if ((job.failed || job.state === "failed") && job.ref) text += `\n問い合わせ番号: ${job.ref}`;
    summary.textContent = text;
  } else {
    summary.textContent = job.cancel_requested ? "中止しています(いまの1件が終わると止まります)..." : "";
  }

  const list = $("print-items");
  list.textContent = "";
  for (const item of job.items || []) {
    const li = document.createElement("li");
    const mk = document.createElement("span");
    mk.className = `mk mk--${item.status}`;
    mk.textContent = MARKS[item.status] || "";
    const body = document.createElement("span");
    body.textContent = `${item.index}. ${item.label}`;
    if (item.message && (item.status === "error" || item.status === "skipped")) {
      const msg = document.createElement("span");
      msg.className = "msg";
      msg.textContent = item.message;
      body.appendChild(msg);
      // 原因(Excel / VBScript が返したもの)。電話で読み上げてもらえるように出す
      if (item.detail) {
        const det = document.createElement("span");
        det.className = "detail";
        det.textContent = item.detail;
        body.appendChild(det);
      }
    }
    li.append(mk, body);
    list.appendChild(li);
  }
  $("print-printer").textContent = job.printer ? `プリンター: ${job.printer}` : "";
  $("print-cancel").hidden = !active;
  $("print-cancel").disabled = !!job.cancel_requested;
  $("print-close").hidden = active;
  if (!active && $("print-dialog").open) $("print-close").focus();

  sheet.setPrinting(active && !other);
  running.set(active ? { label: "印刷", message: current, current: Math.max(job.current_index, 0), total } : null);
}

/** 画面を開いたとき・表に戻ったとき。印刷中なら進み具合を出す。 */
export async function resume() {
  try {
    const data = await api.get("/api/print/status");
    const job = data.job;
    if (!job || job.state === "idle") return;
    if (job.active) { showDialog(); render(job); poll(); return; }
    // 見ていないあいだに終わっていたら、結果を出す
    if (lastJob && lastJob.job_id === job.job_id && lastJob.state !== job.state) { showDialog(); render(job); }
  } catch { /* 繋がっていなければ赤い帯が出ている */ }
}

export function init() {
  $("btn-print").addEventListener("click", start);
  $("print-cancel").addEventListener("click", async () => {
    $("print-cancel").disabled = true;
    try { render((await api.post("/api/print/cancel")).job); } catch (err) { toastError(err); }
  });
  $("print-close").addEventListener("click", () => $("print-dialog").close());
  $("print-dialog").addEventListener("cancel", (ev) => {
    if ($("print-close").hidden) ev.preventDefault();   // 印刷中は閉じない(二重実行防止)
  });
  // Ctrl+P はブラウザの印刷(画面の印刷)ではなく、点検表の印刷にする
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === "p" || ev.key === "P")) {
      ev.preventDefault();
      if (!document.querySelector("dialog[open]")) start();
    }
  });
  const paint = () => {
    const none = sheet.state.selection.length === 0;
    $("btn-preview").disabled = none || sheet.state.printing;
    $("btn-print").disabled = none || sheet.state.printing;
  };
  sheet.onChange(paint);
  paint();
}
