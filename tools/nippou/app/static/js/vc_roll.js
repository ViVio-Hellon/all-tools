/*
  vc_roll.js — VC フィルムのロールを、軸で縦に切った立体の図で描く(SVG)

  外部のライブラリは使わない(ラインPCはインターネットに出られない)。
  vc-calculator の `roll.js` の移植(色は tokens.css の `--vc-roll-*`)。
  寸法はサーバが返した `result.geometry` だけを使い、ここでは何も計算で決めない
  (描く位置の比だけ)。内径と外径の比は実物どおり、高さ(フィルムの幅)は決まった値。

    ┌ 上の面(後ろ半分の楕円)… 紙管の口と、フィルムの巻き
    │ 切り口(軸を通る面)    … 左右にフィルムの帯、真ん中に紙管の壁と中の空洞
    └ 手前半分は点線(切り取った側)

  `data-part` で図の部分に名前を付ける。計算の経過の段にカーソルを当てると、
  その段が使う部分が光る(core 内径 / film 巻き / outer 外径 / layer フィルム1枚)。
*/
const NS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, text = "") {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (text) node.textContent = text;
  return node;
}

const fmt = (v, d = 1) => {
  const t = Number(v).toFixed(d);
  return t.includes(".") ? t.replace(/\.?0+$/, "") : t;
};

/** 上半分の楕円弧(左 → 右、奥側を回る)。 */
function upperArc(cx, cy, rx, ry, reverse = false) {
  return reverse
    ? `A ${rx} ${ry} 0 0 0 ${cx - rx} ${cy}`
    : `A ${rx} ${ry} 0 0 1 ${cx + rx} ${cy}`;
}

export function drawRoll(geometry) {
  const W = 500, Hh = 420;
  const svg = el("svg", { viewBox: `0 0 ${W} ${Hh}`, class: "vc-rollsvg", role: "img",
    "aria-label": `ロールの断面図。内径 ${fmt(geometry.inside)} mm、肉厚 ${fmt(geometry.wall)} mm、`
      + `外径 ${fmt(geometry.outer)} mm、VC厚 ${fmt(geometry.film, 2)} mm で ${fmt(geometry.turns, 0)} 巻` });

  const cx = 190, top = 118, bottom = 290, k = 0.40;
  const R = 150;                                           // 外径の半分(px)
  const r = R * geometry.inside / geometry.outer;          // 内径の半分(実物の比)
  const w = Math.max(5, r * 0.09);                         // 紙管の壁(図のための厚み)
  const ry = (x) => x * k;

  const defs = el("defs");
  const hollow = el("linearGradient", { id: "vcRollHollow", x1: 0, x2: 1, y1: 0, y2: 0 });
  hollow.append(el("stop", { offset: "0", class: "rs-hollow-a" }), el("stop", { offset: "0.5", class: "rs-hollow-b" }),
                el("stop", { offset: "1", class: "rs-hollow-a" }));
  const film = el("linearGradient", { id: "vcRollFilm", x1: 0, x2: 0, y1: 0, y2: 1 });
  film.append(el("stop", { offset: "0", class: "rs-film-a" }), el("stop", { offset: "1", class: "rs-film-b" }));
  defs.append(hollow, film);
  svg.append(defs);

  // --- 切り取った手前半分(点線) ---
  const ghost = el("g", { class: "rs-ghost" });
  ghost.append(
    el("path", { d: `M ${cx - R} ${top} A ${R} ${ry(R)} 0 0 0 ${cx + R} ${top}` }),
    el("path", { d: `M ${cx - R} ${bottom} A ${R} ${ry(R)} 0 0 0 ${cx + R} ${bottom}` }),
  );
  svg.append(ghost);

  // --- 紙管の中(奥の内壁) ---
  const ri = r - w;
  svg.append(el("path", { class: "rs-hollow", "data-part": "core",
    d: `M ${cx - ri} ${top} ${upperArc(cx, top, ri, ry(ri))} L ${cx + ri} ${bottom} `
      + `A ${ri} ${ry(ri)} 0 0 0 ${cx - ri} ${bottom} Z` }));

  // --- 奥の外周の影(後ろ半分の外側。上の面より少し下にずらして厚みを見せる) ---
  svg.append(el("path", { class: "rs-back", d: `M ${cx - R} ${top} ${upperArc(cx, top, R, ry(R))} `
    + `L ${cx + R} ${top + 6} A ${R} ${ry(R)} 0 0 0 ${cx - R} ${top + 6} Z` }));

  // --- 上の面: フィルムの巻き(後ろ半分) ---
  svg.append(el("path", { class: "rs-film-top", "data-part": "film",
    d: `M ${cx - R} ${top} ${upperArc(cx, top, R, ry(R))} L ${cx + r} ${top} `
      + `A ${r} ${ry(r)} 0 0 0 ${cx - r} ${top} Z` }));
  const rings = el("g", { class: "rs-rings", "data-part": "layer" });
  const n = 9;
  for (let i = 1; i < n; i += 1) {
    const rr = r + (R - r) * i / n;
    rings.append(el("path", { d: `M ${cx - rr} ${top} ${upperArc(cx, top, rr, ry(rr))}` }));
  }
  svg.append(rings);
  // 紙管の口
  svg.append(el("path", { class: "rs-core", "data-part": "core",
    d: `M ${cx - r} ${top} ${upperArc(cx, top, r, ry(r))} L ${cx + ri} ${top} `
      + `A ${ri} ${ry(ri)} 0 0 0 ${cx - ri} ${top} Z` }));

  // --- 切り口(軸を通る面) ---
  const cut = el("g", { class: "rs-cut" });
  for (const [x0, x1] of [[cx - R, cx - r], [cx + r, cx + R]]) {
    cut.append(el("rect", { class: "rs-film-cut", "data-part": "film", x: x0, y: top,
                            width: x1 - x0, height: bottom - top }));
    const stripes = el("g", { class: "rs-stripes", "data-part": "layer" });
    for (let i = 1; i < n; i += 1) {
      const x = x0 + (x1 - x0) * i / n;
      stripes.append(el("line", { x1: x, y1: top, x2: x, y2: bottom }));
    }
    cut.append(stripes);
  }
  for (const x0 of [cx - r, cx + r - w]) {
    cut.append(el("rect", { class: "rs-core", "data-part": "core", x: x0, y: top, width: w,
                            height: bottom - top }));
  }
  svg.append(cut);
  svg.append(el("line", { class: "rs-axis", x1: cx, y1: top - 40, x2: cx, y2: bottom + 22 }));

  // --- 寸法 ---
  const dims = el("g", { class: "rs-dim" });
  const arrow = (x1, x2, y, label, part) => {
    const g = el("g", { "data-part": part });
    g.append(el("line", { x1, y1: y, x2, y2: y, "marker-start": "url(#vcRollArrow)",
                          "marker-end": "url(#vcRollArrow)" }),
             el("line", { class: "rs-ext", x1, y1: bottom + 4, x2: x1, y2: y + 5 }),
             el("line", { class: "rs-ext", x1: x2, y1: bottom + 4, x2, y2: y + 5 }),
             el("text", { x: (x1 + x2) / 2, y: y - 5, "text-anchor": "middle" }, label));
    return g;
  };
  const marker = el("marker", { id: "vcRollArrow", viewBox: "0 0 10 10", refX: 5, refY: 5,
                                markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
  marker.append(el("path", { d: "M 0 1 L 9 5 L 0 9 z", class: "rs-arrowhead" }));
  defs.append(marker);
  dims.append(arrow(cx - r, cx + r, bottom + 34, `内径 ${fmt(geometry.inside)} mm`, "core"),
              arrow(cx - R, cx + R, bottom + 72, `外径 ${fmt(geometry.outer)} mm`, "outer"));
  // 肉厚(右の帯の上)
  const wallY = top + (bottom - top) * 0.5;
  const wall = el("g", { "data-part": "film" });
  wall.append(el("line", { x1: cx + r, y1: wallY, x2: cx + R, y2: wallY,
                           "marker-start": "url(#vcRollArrow)", "marker-end": "url(#vcRollArrow)" }),
              el("text", { class: "rs-onfilm", x: cx + (r + R) / 2, y: wallY - 7, "text-anchor": "middle" },
                 `肉厚 ${fmt(geometry.wall)}`));
  dims.append(wall);
  svg.append(dims);

  // --- 拡大(フィルム1枚) ---
  const mx = 430, my = 92, mr = 52;
  const mag = el("g", { class: "rs-mag", "data-part": "layer" });
  mag.append(el("line", { class: "rs-lead", x1: cx + R - 8, y1: top + 30, x2: mx - mr * 0.75, y2: my + mr * 0.66 }));
  const clip = el("clipPath", { id: "vcRollMagClip" });
  clip.append(el("circle", { cx: mx, cy: my, r: mr - 2 }));
  defs.append(clip);
  const inside = el("g", { "clip-path": "url(#vcRollMagClip)" });
  inside.append(el("rect", { class: "rs-film-cut", x: mx - mr, y: my - mr, width: mr * 2, height: mr * 2 }));
  for (let i = -4; i <= 4; i += 1) {
    inside.append(el("line", { class: "rs-layer", x1: mx + i * 11, y1: my - mr, x2: mx + i * 11, y2: my + mr }));
  }
  mag.append(inside, el("circle", { class: "rs-lens", cx: mx, cy: my, r: mr }));
  mag.append(el("text", { x: mx, y: my + mr + 16, "text-anchor": "middle" },
                `VC厚 ${fmt(geometry.film, 2)} mm`),
             el("text", { class: "rs-small", x: mx, y: my + mr + 32, "text-anchor": "middle" },
                `× ${fmt(geometry.turns, 0)} 巻`));
  svg.append(mag);

  // --- 長さ ---
  svg.append(el("text", { class: "rs-title", x: 14, y: 26 },
                `ほどくと ${fmt(geometry.length_m, 1)} m`));
  svg.append(el("text", { class: "rs-small", x: 14, y: 44 },
                `平均の1周 ${fmt(geometry.mean_round, 0)} mm × ${fmt(geometry.turns, 0)} 巻`));
  return svg;
}

/** 経過の段にカーソル・焦点が来たら、図のその部分を光らせる。1度だけ繋ぐ(図は描き直しで入れ替わる)。 */
export function linkHighlight(getSvg, list) {
  const set = (part) => { const svg = getSvg(); if (svg) svg.dataset.hl = part || ""; };
  list.addEventListener("pointerover", (e) => set(e.target.closest("[data-part]")?.dataset.part));
  list.addEventListener("focusin", (e) => set(e.target.closest("[data-part]")?.dataset.part));
  list.addEventListener("pointerleave", () => set(""));
  list.addEventListener("focusout", () => set(""));
}
