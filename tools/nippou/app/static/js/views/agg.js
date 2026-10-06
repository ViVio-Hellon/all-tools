/*
  views/agg.js — 集計管理

  VBA の「集計シート」を人が読む場所にしたもの。**表を組み立てるのは
  サーバ**(`presenters/agg_admin.py`)で、ここは受け取った行を器へ
  流し込むだけ。列や丸め方の決まりを Jinja と JS の2か所に置かない。

  初回に出ている表は Jinja が描いたものなので、**JSが転んでも読めます**。
  ここが受け持つのは、期間を変えたときの差し替えと書き出しだけ。

  【`start()` の中で全部やる理由】
  この画面へ2度目に来たとき、モジュールは読み直されない(ES モジュールは
  一度きり)。**要素は毎回引き直す**必要があるので、配線はここに閉じる。
*/

import { api } from "../api.js";
import { lineLabel } from "../line_label.js";
import { stampAsOf } from "../asof.js";
import { paintTimingChart } from "../chart.js";
import { paintTable } from "../table.js";
import { toast, toastError } from "../toast.js";

function showNote(text, kind = "info") {
  const box = document.getElementById("agg-note");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

/*
  共有保存の履歴(押した時刻と担当者)。**表とグラフを組み立てるのはサーバ**
  (`presenters/push_log.py`)。画面を出してから取りに行きます ── 全端末の
  行は共有の日報データ.sqlite3 にしかなく、共有が遅いときに画面ごと
  待たせないため。
*/
/** 履歴のカードの知らせ。**押したボタンの近くに出す**(上の知らせは遠い) */
function pushNote(text, kind = "info") {
  const box = document.getElementById("push-log-msg");
  if (!box) return;
  box.hidden = !text;
  box.textContent = text;
  box.className = `msg msg--${kind}`;
}

function paintPushLog(view) {
  paintTable(view.table);
  paintTimingChart(document.getElementById("push-log-chart"), view.chart);
  const counts = document.getElementById("push-log-counts");
  if (counts) {
    // 並びはサーバが決めた順(押した回数 → 直内 → 直の後 → 結果)
    counts.textContent = (view.counts || [])
      .map(([label, n]) => (label === "押した" ? `押した ${n}回` : `${label} ${n}`))
      .join(" / ");
  }
  const source = document.getElementById("push-log-source");
  if (source) source.textContent = `出どころ: ${view.source}`;
  const warning = document.getElementById("push-log-warning");
  if (warning) {
    warning.hidden = !view.warning;
    warning.textContent = view.warning || "";
  }
}

export function start() {
  const startInput = document.getElementById("start");
  const endInput = document.getElementById("end");
  const span = () => ({ start: startInput?.value || "",
                        end: endInput?.value || "" });
  /*
    共有保存の履歴の期間。**このカードの開始日・終了日**(v3.86.0) ──
    上の表の期間に付いていくと、遠くの欄を探して打つことになる。
  */
  const pushStart = document.getElementById("push-log-start");
  const pushEnd = document.getElementById("push-log-end");
  const pushSpan = () => ({
    start: pushStart?.value || "", end: pushEnd?.value || "",
    scope: document.getElementById("push-log-scope")?.value || "line",
  });
  /**
   * いま出している幅に合うボタンに印を付ける(打ち直したら外れる)。
   *
   * **印は1つだけ。** 30日ある月の30日は「今月」と「直近30日」が同じ幅に
   * なり、2つとも押されて見えていました。押したボタンがまだ合っていれば
   * そちら、そうでなければ並びの先のほう。
   */
  let pickedPreset = "";
  const markPreset = () => {
    const buttons = [...document.querySelectorAll("#push-log-presets [data-preset]")];
    const fits = buttons.filter((b) =>
      b.dataset.start === pushStart?.value && b.dataset.end === pushEnd?.value);
    const chosen = fits.find((b) => b.dataset.preset === pickedPreset) || fits[0];
    for (const btn of buttons) {
      btn.setAttribute("aria-pressed", btn === chosen ? "true" : "false");
    }
  };

  async function loadPushLog() {
    if (!document.getElementById("push-log")) return;
    markPreset();
    try {
      paintPushLog(await api.post("/api/agg/push-log", pushSpan()));
    } catch (err) {
      const warning = document.getElementById("push-log-warning");
      if (warning) { warning.hidden = false; warning.textContent = err.message; }
    }
  }

  document.getElementById("reload")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/agg/tables", span());
      for (const table of body.tables || []) paintTable(table);
      // 表だけ新しくして時刻を置き去りにしない(graph.js と同じ)
      stampAsOf(body.generated_at);
      showNote(`${body.start} 〜 ${body.end} / ${lineLabel(body.line)}`, "ok");
    } catch (err) {
      showNote(err.message, "error");
      toastError(err.message);
    }
  });

  document.getElementById("push-log-reload")?.addEventListener("click", loadPushLog);
  document.getElementById("push-log-scope")?.addEventListener("change", loadPushLog);
  // 日付を選び直したら、そのまま引き直す(「表示」を押し忘れて古い表を読まない)
  pushStart?.addEventListener("change", loadPushLog);
  pushEnd?.addEventListener("change", loadPushLog);
  for (const btn of document.querySelectorAll("#push-log-presets [data-preset]")) {
    btn.addEventListener("click", () => {
      pickedPreset = btn.dataset.preset || "";
      if (pushStart) pushStart.value = btn.dataset.start || "";
      if (pushEnd) pushEnd.value = btn.dataset.end || "";
      loadPushLog();
    });
  }
  document.getElementById("push-log-csv")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/agg/push-log/csv", pushSpan());
      pushNote(body.message, "ok");
      toast("共有保存の履歴をCSVに書き出しました");
    } catch (err) {
      pushNote(err.message, "error");
      toastError(err.message);
    }
  });
  loadPushLog();


  document.getElementById("csv")?.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/agg/csv", span());
      showNote(body.message, "ok");
      toast("集計CSVを書き出しました");
    } catch (err) {
      showNote(err.message, "error");
      toastError(err.message);
    }
  });
}
