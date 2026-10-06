/*
  chart.js — 折れ線と棒を SVG で描く

  tkinter版 `ui/graph_canvas.py`(Canvasに自分で線を引く)の置き換え。

  **何を出すか・どう集計するかはサーバが決める**(`logic/aggregation.py`)。
  ここは受け取った「ラベルと値の並び」を座標へ写す純関数だけを持つ。

  集計画面(`views/graph.js`)と直終わりの確認(`views/review.js`)の
  両方が使う ── **同じ絵を2か所で描かない**。
*/

const CHART_W = 720, CHART_H = 220;
const W = CHART_W, H = CHART_H;
const PAD = { top: 16, right: 16, bottom: 34, left: 52 };

const SVG_NS = "http://www.w3.org/2000/svg";

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (text !== "") node.textContent = text;
  return node;
}

/** 値の並びから、目盛りの上限を決める。0のときも軸が潰れないようにする。 */
function ceiling(values) {
  const max = Math.max(0, ...values.filter((v) => Number.isFinite(v)));
  if (max <= 0) return 1;
  const digits = Math.pow(10, Math.floor(Math.log10(max)));
  return Math.ceil(max / digits) * digits;
}

/**
 * 1枚のグラフ。棒でも折れ線でも同じ骨格を使う。
 *
 * `size` を渡すと、その大きさ(座標の単位)で描きます(`paintCharts` の
 * `fit`)。渡さなければ 720×220 で、器の幅いっぱいに伸びます。
 */
function chart(series, labels, size = null) {
  const W = size?.width || CHART_W;
  const H = size?.height || CHART_H;
  const svg = el("svg", {
    class: "chart", viewBox: `0 0 ${W} ${H}`,
    role: "img", "aria-label": `${series.title}(${series.unit})`,
  });

  const values = series.values || [];
  // **頭は目標込みで決める。** 目標が実績より上のとき、目標込みで
  // 見ないと線が枠の外へ出て、届いていないことが読めなくなる
  const top = ceiling([...values, ...(series.target || [])]);
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const y = (v) => PAD.top + plotH - (Math.max(0, v) / top) * plotH;

  // 目盛り(横線)。**数字だけでなく線も引く** ── 棒の高さを目で比べる
  for (let i = 0; i <= 4; i += 1) {
    const value = (top / 4) * i;
    const yy = y(value);
    svg.appendChild(el("line", { class: "gridline", x1: PAD.left, x2: W - PAD.right, y1: yy, y2: yy }));
    svg.appendChild(el("text", { class: "label", x: PAD.left - 6, y: yy + 3,
                                 "text-anchor": "end" }, String(Math.round(value * 100) / 100)));
  }
  svg.appendChild(el("line", { class: "axis", x1: PAD.left, x2: PAD.left,
                               y1: PAD.top, y2: PAD.top + plotH }));

  if (!values.length) {
    svg.appendChild(el("text", { class: "label", x: W / 2, y: H / 2,
                                 "text-anchor": "middle" }, "この期間のデータはありません"));
    return svg;
  }

  const step = plotW / values.length;
  if (series.kind === "bar") {
    const width = Math.min(48, step * 0.6);
    // 棒1本ずつの印。**色を決めるのはサーバ**(`Tile.point_classes`)で、
    // ここは受け取った class 名を書くだけ。直別の棒が直の色になるのは
    // これのおかげで、JSは「1直が何色か」を知らない
    const marks = series.pointClasses || [];
    values.forEach((v, i) => {
      const cx = PAD.left + step * (i + 0.5);
      const yy = y(v);
      const mark = marks[i] ? ` mark-${marks[i]}` : "";
      svg.appendChild(el("rect", { class: `bar${mark}`, x: cx - width / 2,
                                   y: yy, width,
                                   height: PAD.top + plotH - yy }));
    });
  } else {
    const points = values.map((v, i) =>
      `${PAD.left + step * (i + 0.5)},${y(v)}`).join(" ");
    svg.appendChild(el("polyline", { class: "line", points }));
    values.forEach((v, i) => {
      svg.appendChild(el("circle", { class: "dot", r: DOT_R,
                                     cx: PAD.left + step * (i + 0.5), cy: y(v) }));
    });
  }

  /*
    45度線(目標値)。**実績のあとに重ねる** ── 先に引くと棒や折れ線の
    下に隠れます。目盛りは実績と同じものを使うので、目標が実績より
    大きいときは `ceiling` が目標まで伸びている必要があります
    (`paintTile` が目標込みで頭を決めています)。
  */
  const target = series.target || [];
  if (target.length) {
    const points = target.map((v, i) =>
      `${PAD.left + step * (i + 0.5)},${y(v)}`).join(" ");
    svg.appendChild(el("polyline", { class: "line line--target", points }));
  }

  // 横軸のラベル。**多いときは間引く** ── 重なって読めないほうが困る
  const every = Math.ceil(labels.length / 12) || 1;
  labels.forEach((text, i) => {
    if (i % every !== 0) return;
    svg.appendChild(el("text", {
      class: "label", x: PAD.left + step * (i + 0.5), y: H - 12,
      "text-anchor": "middle",
    }, String(text).replace(/^\d+年/, "")));
  });

  return svg;
}

/* ==================================================================
   複合グラフ ── 棒(その日の枚数) + 折れ線(累積枚数)

   **目盛りを左右で分ける。** その日の枚数は数百、累積は数万になるので、
   同じ目盛りに乗せると棒が床に貼り付いて高さの差が読めません。左が棒、
   右が折れ線で、どちらの軸の数字かは軸の位置で分かります。

   軸をどちらにするかは**サーバが決めます**(`series[].axis`)。JSは
   座標へ写すだけ、という決まりをここでも守ります。
   ================================================================== */
function combo(tile) {
  const labels = tile.labels || [];
  const series = tile.series || [];
  const svg = el("svg", {
    class: "chart", viewBox: `0 0 ${W} ${H}`,
    role: "img", "aria-label": `${tile.title}(${tile.unit})`,
  });

  const pick = (axis) => series.filter((s) => (s.axis || "left") === axis);
  /*
    目盛りの頭。**積み上げるぶんは足してから測る。**

    積み上げた棒は、いちばん高い1本ぶんではなく**積んだ高さ**まで
    伸びます ── 足さずに測ると、棒が枠を突き抜けます。
  */
  const topOf = (axis) => {
    const rows = pick(axis);
    const stacked = new Map();          // 積む名前 → 添字ごとの合計
    const loose = [];
    for (const s of rows) {
      if (s.kind === "bar" && s.stack) {
        const sums = stacked.get(s.stack) || [];
        (s.values || []).forEach((v, i) => {
          sums[i] = (sums[i] || 0) + Math.max(0, v || 0);
        });
        stacked.set(s.stack, sums);
      } else {
        loose.push(...(s.values || []));
      }
    }
    return ceiling([...loose, ...[...stacked.values()].flat()]);
  };
  const left = topOf("left");
  const right = topOf("right");
  // 右に数字を出すぶん、右の余白を広げる。**桁数ぶんだけ** ──
  // 固定値だと、累積が5桁になった日に数字が切れる(実際に切れた)
  const rightPad = pick("right").length
    ? Math.max(PAD.right, 12 + String(Math.round(right)).length * 7)
    : PAD.right;
  const plotW = W - PAD.left - rightPad;
  const plotH = H - PAD.top - PAD.bottom;
  const y = (v, axis) =>
    PAD.top + plotH - (Math.max(0, v) / ((axis === "right") ? right : left)) * plotH;

  // 目盛り。**左右とも数字を出す** ── 片方しか無いと、折れ線がどの
  // 大きさなのか読めない
  for (let i = 0; i <= 4; i += 1) {
    const yy = PAD.top + plotH - (plotH / 4) * i;
    svg.appendChild(el("line", { class: "gridline", x1: PAD.left,
                                 x2: W - rightPad, y1: yy, y2: yy }));
    svg.appendChild(el("text", { class: "label", x: PAD.left - 6, y: yy + 3,
                                 "text-anchor": "end" },
                       String(Math.round((left / 4) * i))));
    if (pick("right").length) {
      svg.appendChild(el("text", { class: "label", x: W - rightPad + 6,
                                   y: yy + 3, "text-anchor": "start" },
                         String(Math.round((right / 4) * i))));
    }
  }
  svg.appendChild(el("line", { class: "axis", x1: PAD.left, x2: PAD.left,
                               y1: PAD.top, y2: PAD.top + plotH }));

  const count = labels.length;
  if (!count) {
    svg.appendChild(el("text", { class: "label", x: W / 2, y: H / 2,
                                 "text-anchor": "middle" },
                       tile.empty || "この期間のデータはありません"));
    return svg;
  }

  const step = plotW / count;
  /*
    積み上げた棒の**足もと**。積む名前ごとに、添字ごとの「ここまで
    積んだ高さ」を覚えておきます。

    棒は下から 1直 → 2直 → 3直 の順に積みます(`SHIFT_STACK_ORDER`)。
    **並べる順を決めるのはサーバ**で、ここは渡された順に積むだけです。
  */
  const base = new Map();
  const baseOf = (s, i) => {
    if (!s.stack) return 0;
    const sums = base.get(s.stack) || [];
    return sums[i] || 0;
  };
  const addBase = (s, i, v) => {
    if (!s.stack) return;
    const sums = base.get(s.stack) || [];
    sums[i] = (sums[i] || 0) + Math.max(0, v || 0);
    base.set(s.stack, sums);
  };

  series.forEach((s) => {
    const axis = s.axis || "left";
    const values = s.values || [];
    if (s.kind === "bar") {
      const width = Math.min(48, step * 0.6);
      values.forEach((v, i) => {
        const cx = PAD.left + step * (i + 0.5);
        const from = baseOf(s, i);
        // 足もとから、そのぶんだけ積む。**床ではなく足もとが起点**
        const yy = y(from + Math.max(0, v || 0), axis);
        const height = y(from, axis) - yy;
        addBase(s, i, v);
        // 高さ0の段は描かない ── 0枚の直に、線だけが残ります
        if (height <= 0) return;
        // 段のあいだに地の色の細い境を入れる(`.bar--seg`)。濃淡だけを
        // 頼りにしない ── 隣どうしの明度差は 1.4〜1.7 しかありません
        const seg = s.stack ? " bar--seg" : "";
        svg.appendChild(el("rect", { class: `bar ${markOf(s)}${seg}`,
                                     x: cx - width / 2, y: yy, width,
                                     height }));
      });
    } else {
      // 45度線は実績のなかま色ではなく**目標の見た目**(灰の破線)に
      // する。実績と同じ色にすると、目標も実績に見える
      const target = s.key === "target";
      const cls = target ? "line line--target" : `line ${markOf(s)}`;
      const points = values.map((v, i) =>
        `${PAD.left + step * (i + 0.5)},${y(v, axis)}`).join(" ");
      // **線を2回描く。** 1本目は地の色で太く(縁取り)、2本目が本体。
      //
      // SVG の線には外側の縁取りが無いので、こうするしかありません。
      // 縁取りが無いと、線が棒を横切るところで**棒に溶けて消えます**
      // ── 色を2段に分けても、重なった1本ぶんの幅では足りません。
      svg.appendChild(el("polyline", { class: "line line--halo", points }));
      svg.appendChild(el("polyline", { class: cls, points }));
      if (target) return;                 // 目標に点は打たない(実績と紛れる)
      values.forEach((v, i) => {
        // 点にも地の色の縁を付ける(理由は線と同じ)。**大きめ**に取るのは
        // 指定(marker size 8)で、直径8 ≒ 半径4
        svg.appendChild(el("circle", { class: `dot ${markOf(s)}`, r: DOT_R,
                                       cx: PAD.left + step * (i + 0.5),
                                       cy: y(v, axis) }));
      });
    }
  });

  const every = Math.ceil(count / 12) || 1;
  labels.forEach((text, i) => {
    if (i % every !== 0) return;
    svg.appendChild(el("text", {
      class: "label", x: PAD.left + step * (i + 0.5), y: H - 12,
      "text-anchor": "middle",
    }, String(text).replace(/^\d+年/, "")));
  });
  return svg;
}

/**
 * その系列の**なかまの印**。
 *
 * サーバが `mark`(`qty` / `dur` / `stop`)で指します。色そのものは
 * `tokens.css` が決めるので、ここは class 名を書くだけ ── JSは
 * 「枚数が何色か」を知りません。
 *
 * 指されていなければ何も付けません(そのタイルの種類色が効く)。
 */
/** 折れ線の点の大きさ。指定は marker size 8 なので、半径は4。 */
const DOT_R = 4;

function markOf(s) {
  return s && s.mark ? `mark-${s.mark}` : "";
}

/**
 * 複合グラフの凡例。どの色がどちらの軸かまで書く。
 *
 * **見本の形を、絵の中の形に合わせる。** 棒は四角、線は横棒、目標は
 * 破線 ── 同じなかま(同じ色)の2つが並んだとき、これが唯一の手がかりに
 * なります。色で分けるのをやめた以上、ここを四角で揃えたら読めません。
 */
function comboLegend(tile) {
  const list = document.createElement("ul");
  list.className = "dlg";
  (tile.series || []).forEach((s) => {
    const item = document.createElement("li");
    const swatch = document.createElement("span");
    const shape = s.kind === "bar" ? "" : " dlg__swatch--line";
    swatch.className = s.key === "target"
      ? "dlg__swatch dlg__swatch--target"
      : `dlg__swatch${shape} ${markOf(s)}`;
    const text = document.createElement("span");
    text.className = "dlg__text";
    const side = (s.axis || "left") === "right" ? "右目盛り" : "左目盛り";
    text.textContent = `${s.title}(${s.unit}・${side})`;
    item.append(swatch, text);
    list.append(item);
  });
  return list;
}

/**
 * ビューモデルの `series` を並べて描く。**器は呼び手が用意する。**
 *
 * `fit: true` なら、**1枚ずつ自分の枠の大きさで描きます**(直の実績)。
 *
 *     大きすぎて見えません 画面最大化で収まるサイズに表示してください
 *
 * 720×220 の絵を幅いっぱいに伸ばすと、幅 1900px の画面では1枚が
 * 高さ 560px になり、3枚で画面の2倍を超えていました。枠(`.fit-chart__plot`)
 * の高さは CSS が画面から決め、絵はその枠に合わせて描き直します ──
 * 伸ばすのではなく描き直すので、字の大きさは枠が狭くても保たれます。
 */
export function paintCharts(host, history, { fit = false } = {}) {
  if (!host) return;
  host.replaceChildren();
  host.classList.add("chart-skin");
  for (const series of history.series || []) {
    const head = document.createElement("h3");
    head.textContent = `${series.title}(${series.unit})`;
    if (!fit) {
      head.style.marginTop = "var(--sp-3)";
      host.append(head, chart(series, history.labels || []));
      continue;
    }
    const box = document.createElement("figure");
    box.className = "fit-chart";
    const plot = document.createElement("div");
    plot.className = "fit-chart__plot";
    box.append(head, plot);
    host.append(box);
    fitChart(plot, (size) => chart(series, history.labels || [], size));
  }
}

/** 枠が大きいときに字も少し大きくする上限(遠くから見る画面なので)。 */
const FIT_ZOOM_MAX = 1.6;

/**
 * 枠の大きさで描き、**枠が変わったら描き直す**(全画面・窓の大きさ)。
 *
 * 枠が大きいときは、少し小さく描いて拡大します(字も線も最大 1.6倍)。
 * 縦横の比は枠と同じなので、絵が歪んだり余白が出たりはしません。
 */
function fitChart(plot, draw) {
  let last = "";
  const paint = () => {
    const w = plot.clientWidth, h = plot.clientHeight;
    if (!w || !h) return;                   // 隠れている間は描かない
    const key = `${w}x${h}`;
    if (key === last) return;
    last = key;
    const zoom = Math.min(FIT_ZOOM_MAX, Math.max(1, Math.min(w / CHART_W, h / 200)));
    plot.replaceChildren(draw({ width: Math.round(w / zoom),
                                height: Math.round(h / zoom) }));
  };
  paint();
  if (typeof ResizeObserver === "function") {
    let queued = false;
    new ResizeObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; paint(); });
    }).observe(plot);
  }
}

/* ==================================================================
   ドーナツ (VBA `停止グラフ挿入` の円グラフ)

   **内訳は輪で見せる。** 「突発が何分か」より「その日の止まりのうち
   どれが大きいか」を知りたいので、割合が形になるほうが早い。真ん中に
   合計を置くのは、割合だけ見て「で、何分なの」と探させないため。

   **色はサーバが指す印(`slice.mark`)だけで決まります。** 以前は印が
   無ければ8色を順番に回していましたが、そうすると長い順に並べ替えた
   拍子に同じ項目が日によって違う色になり、色が何も意味しなくなります。
   印が無ければ**そのグラフのなかまの色**(1色)で塗り、見分けは直接
   ラベルと隙間に任せます。

   色の出どころはトークンの1か所(`tokens.css`)。ここでは決めません。
   ================================================================== */
const DONUT = { size: 200, thickness: 26 };

/** 扇1つの色の印。**サーバが指したものだけ。** */
function sliceClass(slice) {
  return slice && slice.mark ? `mark-${slice.mark}` : "";
}

/** 中心角から、円周上の点。12時から時計回り。 */
function polar(cx, cy, r, ratio) {
  const angle = (ratio * 360 - 90) * (Math.PI / 180);
  return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
}

/** ドーナツ1枚。`slices` は `{label, value, pct}` の並び(サーバが決める)。 */
function donut(tile) {
  const size = DONUT.size;
  const cx = size / 2, cy = size / 2;
  const outer = size / 2 - 2;
  const inner = outer - DONUT.thickness;

  const svg = el("svg", {
    class: "donut", viewBox: `0 0 ${size} ${size}`,
    role: "img", "aria-label": `${tile.title}(${tile.unit})`,
  });

  const slices = tile.slices || [];
  const total = slices.reduce((sum, s) => sum + (s.value || 0), 0);
  if (!total) {
    svg.appendChild(el("circle", { class: "donut__empty", cx, cy,
                                   r: (outer + inner) / 2,
                                   "stroke-width": DONUT.thickness }));
    return svg;
  }

  let from = 0;
  slices.forEach((slice, i) => {
    const ratio = (slice.value || 0) / total;
    const to = from + ratio;
    // まるごと1色のときは弧では描けない(始点と終点が同じ)ので輪にする
    if (ratio >= 0.999) {
      svg.appendChild(el("circle", {
        class: `donut__ring ${sliceClass(slice)}`, cx, cy,
        r: (outer + inner) / 2, "stroke-width": DONUT.thickness,
      }));
    } else {
      const [x1, y1] = polar(cx, cy, outer, from);
      const [x2, y2] = polar(cx, cy, outer, to);
      const [x3, y3] = polar(cx, cy, inner, to);
      const [x4, y4] = polar(cx, cy, inner, from);
      const large = ratio > 0.5 ? 1 : 0;
      svg.appendChild(el("path", {
        class: `donut__slice ${sliceClass(slice)}`,
        d: `M${x1},${y1} A${outer},${outer} 0 ${large} 1 ${x2},${y2}`
           + ` L${x3},${y3} A${inner},${inner} 0 ${large} 0 ${x4},${y4} Z`,
      }));
    }
    from = to;
  });

  // 真ん中の合計。**割合だけ見せて終わりにしない**
  svg.appendChild(el("text", { class: "donut__total", x: cx, y: cy + 2,
                               "text-anchor": "middle" }, tile.total || ""));
  svg.appendChild(el("text", { class: "donut__unit", x: cx, y: cy + 20,
                               "text-anchor": "middle" }, tile.unit || ""));
  return svg;
}

/** ドーナツの凡例。**色だけでは何の色か分からない。** */
function legend(tile) {
  const list = document.createElement("ul");
  list.className = "dlg";
  (tile.slices || []).forEach((slice, i) => {
    const item = document.createElement("li");
    const swatch = document.createElement("span");
    swatch.className = `dlg__swatch ${sliceClass(slice)}`;
    const text = document.createElement("span");
    text.className = "dlg__text";
    text.textContent = `${slice.label} ${slice.value}${tile.unit}`;
    const pct = document.createElement("b");
    pct.textContent = `${slice.pct}%`;
    item.append(swatch, text, pct);
    list.append(item);
  });
  return list;
}

/**
 * タイル1枚の中身を描く。
 *
 * 数字と表は**サーバが描いたものがすでに入っている**ので触りません
 * (JSが止まっても読めるように)。ここが受け持つのは絵だけ。
 */
export function paintTile(host, tile) {
  if (!host || !tile) return;
  host.replaceChildren();
  if (!tile.has_data) {
    const note = document.createElement("p");
    note.className = "lead tile__empty";
    note.textContent = tile.empty || "データがありません";
    // **板は外す。** 絵が無いのに濃紺だけ残ると、読み込み中に見えます
    host.classList.remove("chart-skin");
    host.append(note);
    return;
  }
  // 絵と凡例は**同じ濃紺の板の上**に載せる(`.chart-skin`)。凡例だけ
  // 白い地に出すと、蛍光色の見本が読めません(緑や水色がとくに)
  host.classList.add("chart-skin");
  if (tile.kind === "donut") {
    host.append(donut(tile), legend(tile));
    return;
  }
  if (tile.kind === "combo") {
    host.append(combo(tile), comboLegend(tile));
    return;
  }
  if (tile.kind === "bar" || tile.kind === "line") {
    host.append(chart({ title: tile.title, unit: tile.unit,
                        kind: tile.kind, values: tile.values,
                        // 棒1本ずつの色の印(直別は直の色)。サーバが決める
                        pointClasses: tile.point_classes || [],
                        // 45度線。**目盛りの頭は目標込みで決める** ──
                        // 目標が実績より上だと、線が枠の外へ出てしまう
                        target: tile.target || [] },
                      tile.labels || []));
    if ((tile.target || []).length) host.append(targetLegend(tile));
  }
}

/** 目標線の凡例。**赤い線が何なのかを、色だけに頼らずに書く。** */
function targetLegend(tile) {
  const list = document.createElement("div");
  list.className = "dlg";
  const item = document.createElement("div");
  item.className = "dlg__item";
  const swatch = document.createElement("span");
  swatch.className = "dlg__swatch dlg__swatch--target";
  const text = document.createElement("span");
  text.textContent = tile.target_title || "目標値";
  item.append(swatch, text);
  list.append(item);
  return list;
}

export { chart, combo, donut };

/* ==================================================================
   共有保存を押したタイミング (`presenters/push_log.py`)

   **直の終わりから何分後に押したか**を、日ごと・直ごとの点で並べる。

       縦   直の終わりを 0。上が「終わってから」、下が「終わる前」
       横   報告日。1日の幅の中で 1直・2直・3直・日勤を左から並べる
       色と形  直(●■◆▲)── 色だけに頼らない(凡例もある)
       塗り    塗り = 押したあと共有に出ている / 白抜き = 出ていない

   **何をどこに置くかはサーバが決める。** 範囲の外の点(何時間も後)は
   サーバが端に寄せて `clipped` を付けてくるので、ここは矢印を添えるだけ。
   点に載せると(`title`)、担当者・押した時刻・結果が出る。
   ================================================================== */
const TIMING = { W: 720, H: 260, top: 14, right: 12, bottom: 30, left: 64 };
const SHIFT_SLOTS = ["1直", "2直", "3直", "日勤"];

/** 目盛りの字。**下は「◯分前」(直内)、上は「◯分後」(直が終わってから)。** */
function hoursLabel(minutes) {
  if (minutes === 0) return "直の終わり";
  const side = minutes > 0 ? "後" : "前";
  const m = Math.abs(minutes);
  if (m % 60 === 0) return `${m / 60}時間${side}`;
  return m < 60 ? `${m}分${side}` : `${Math.floor(m / 60)}時間${m % 60}分${side}`;
}

/** 形1つ。**大きさは 8px 以上**(小さい点は見分けられない)。 */
function shapeAt(shape, x, y, cls) {
  const r = 5;
  if (shape === "square") {
    return el("rect", { class: cls, x: x - r, y: y - r, width: r * 2, height: r * 2, rx: 1 });
  }
  if (shape === "diamond") {
    const d = r + 1;
    return el("polygon", { class: cls, points: `${x},${y - d} ${x + d},${y} ${x},${y + d} ${x - d},${y}` });
  }
  if (shape === "triangle") {
    const d = r + 1;
    return el("polygon", { class: cls, points: `${x},${y - d} ${x + d},${y + d * 0.8} ${x - d},${y + d * 0.8}` });
  }
  return el("circle", { class: cls, cx: x, cy: y, r });
}

function timingSvg(chart) {
  const { W, H } = TIMING;
  const svg = el("svg", { class: "chart timing", viewBox: `0 0 ${W} ${H}`,
                          role: "img", "aria-label": chart.title || "" });
  const plotW = W - TIMING.left - TIMING.right;
  const plotH = H - TIMING.top - TIMING.bottom;
  const lo = chart.y_min, hi = chart.y_max;
  const y = (m) => TIMING.top + plotH - ((m - lo) / Math.max(1, hi - lo)) * plotH;
  const days = chart.days || [];
  const band = plotW / Math.max(1, days.length);

  // 目盛り: 範囲に合わせて 15分 / 30分 / 1時間。**0 の線だけ太く**して名前を付ける
  const span = hi - lo;
  const step = span <= 120 ? 15 : span <= 240 ? 30 : 60;
  for (let m = Math.ceil(lo / step) * step; m <= hi; m += step) {
    const yy = y(m);
    svg.appendChild(el("line", { class: m === 0 ? "timing__zero" : "gridline",
                                 x1: TIMING.left, x2: W - TIMING.right, y1: yy, y2: yy }));
    svg.appendChild(el("text", { class: "label", x: TIMING.left - 6, y: yy + 3,
                                 "text-anchor": "end" }, hoursLabel(m)));
  }
  svg.appendChild(el("line", { class: "axis", x1: TIMING.left, x2: TIMING.left,
                               y1: TIMING.top, y2: TIMING.top + plotH }));

  // 線の上下の読み方は凡例に書く。**絵の中に置くと、最新の日の点と重なる**

  // 日付。**混み合うときは間引く**(ラベルどうしが重ならない数だけ)
  const every = Math.max(1, Math.ceil(days.length / 16));
  days.forEach((label, i) => {
    if (i % every) return;
    svg.appendChild(el("text", { class: "label", x: TIMING.left + band * (i + 0.5),
                                 y: H - 10, "text-anchor": "middle" }, label));
  });

  if (!(chart.points || []).length) {
    svg.appendChild(el("text", { class: "label", x: W / 2, y: H / 2,
                                 "text-anchor": "middle" }, chart.empty || ""));
    return svg;
  }

  for (const p of chart.points) {
    const slot = Math.max(0, SHIFT_SLOTS.indexOf(p.shift));
    const x = TIMING.left + band * p.day + band * (0.2 + 0.2 * slot);
    const yy = y(p.minutes);
    const group = el("g", { class: "timing__pt" });
    group.appendChild(el("title", {}, p.tip + (p.clipped ? "\n(グラフの外なので端に置いています)" : "")));
    // 当たり判定は形より大きく。細い点でも載せやすいように
    group.appendChild(el("circle", { class: "timing__hit", cx: x, cy: yy, r: 10 }));
    group.appendChild(shapeAt(p.shape, x, yy,
      `pt mark-${p.mark}${p.reached ? "" : " is-open"}`));
    if (p.clipped) {
      // 端に寄せた点。**本当の値を横に書く**(グラフの外にあることが分かるように)
      group.appendChild(el("text", { class: "label timing__clip", x: x + 8,
                                     y: yy + (p.actual > 0 ? 10 : -3) },
                           `${p.actual > 0 ? "↑" : "↓"}${p.clip_label}`));
    }
    svg.appendChild(group);
  }
  return svg;
}

/** 凡例。**形と色の両方**を見本に出す(色だけで見分けさせない)。 */
function timingLegend(chart) {
  const list = document.createElement("ul");
  list.className = "dlg timing__legend";
  for (const s of chart.shifts || []) {
    const item = document.createElement("li");
    const svg = el("svg", { class: "timing__swatch", viewBox: "0 0 16 16", width: 16, height: 16 });
    svg.appendChild(shapeAt(s.shape, 8, 8, `pt mark-${s.mark}`));
    const text = document.createElement("span");
    text.className = "dlg__text";
    text.textContent = s.name;
    item.append(svg, text);
    list.append(item);
  }
  // 線(直の終わり)の上下の読み方。**ふつうは下(直内)**
  if (chart.below || chart.above) {
    const side = document.createElement("li");
    side.className = "timing__fill-note";
    side.textContent = `線より下 = ${chart.below || ""} / 上 = ${chart.above || ""}`;
    list.append(side);
  }
  const fill = document.createElement("li");
  fill.className = "timing__fill-note";
  fill.textContent = "塗り = 押したあと共有に出ている / 白抜き = 出ていない(関門で止めた・送れなかった)";
  list.append(fill);
  return list;
}

/** 共有保存を押したタイミングのグラフ。**器は呼び手が用意する。** */
export function paintTimingChart(host, chart) {
  if (!host) return;
  host.replaceChildren();
  if (!chart) return;
  const head = document.createElement("h3");
  head.textContent = chart.title || "";
  head.style.marginTop = "var(--sp-2)";
  host.append(head, timingSvg(chart), timingLegend(chart));
}
