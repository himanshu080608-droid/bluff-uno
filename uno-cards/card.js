// ---------------------------------------------------------------------------
// UNO Reverse Card — vanilla HTML/CSS/JS port
// Converted from UnoReverseCard.tsx (React/TypeScript)
// ---------------------------------------------------------------------------

const CARD_COLORS = {
  red:    "#D52B1E",
  yellow: "#F5C800",
  blue:   "#0057A8",
  green:  "#009944",
};

// ---------------------------------------------------------------------------
// SVG path constants
// ---------------------------------------------------------------------------

// Full arrow outline (fill region)
const ARROW_PATH =
  "M -36,11 " +
  "L 8,11 " +
  "L 8,24 L 40,0 L 8,-24 " +
  "L 8,-11 " +
  "C -25,-11 -36,-11 -36,11 " +
  "Z";

// The outer J-curve segment only (drawn as a thickening stroke behind the fill)
const JCURVE_PATH = "M 8,-11 C -25,-11 -36,-11 -36,11";

// The V-shaped arrowhead slant edges (tip at 40,0; ends at 8,±24)
const ARROWHEAD_TIP_PATH = "M 8,24 L 40,0 L 8,-24";

// The two vertical inner-wing segments at x=8
const INNER_WINGS_PATH = "M 8,-24 L 8,-11 M 8,11 L 8,24";

// ---------------------------------------------------------------------------
// Unique-ID counter (replaces React's useId)
// ---------------------------------------------------------------------------
let _idCounter = 0;
function uid(prefix) {
  return `${prefix}-${++_idCounter}`;
}

// ---------------------------------------------------------------------------
// SVG element helper
// ---------------------------------------------------------------------------
function svgEl(tag, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) {
    el.setAttribute(k, String(v));
  }
  return el;
}

// ---------------------------------------------------------------------------
// Tapered J-curve
// ---------------------------------------------------------------------------
// The J-curve Bézier: P0=(8,-11) P1=(-25,-11) P2=(-36,-11) P3=(-36,11)
// Parametric form:
//   x(t) = 8 − 99t + 66t² − 11t³
//   y(t) = −11 + 22t³
function jCurveBezierPoint(t) {
  return [
    8 - 99 * t + 66 * t * t - 11 * t * t * t,
    -11 + 22 * t * t * t,
  ];
}

// Draws the J-curve as N short line segments with strokeWidth growing from 0
// at the shoulder end (t=0) to maxWidth at the tail end (t=1).
function createTaperedJCurve(maxWidth) {
  const N = 16;
  const g = svgEl("g");
  for (let i = 0; i < N; i++) {
    const t0 = i / N;
    const t1 = (i + 1) / N;
    const [x0, y0] = jCurveBezierPoint(t0);
    const [x1, y1] = jCurveBezierPoint(t1);
    const sw = t1 * maxWidth;
    const line = svgEl("line", {
      x1: x0, y1: y0, x2: x1, y2: y1,
      stroke: "black",
      "stroke-width": sw,
      "stroke-linecap": "round",
    });
    g.appendChild(line);
  }
  return g;
}

// ---------------------------------------------------------------------------
// Arrow symbol SVG
// ---------------------------------------------------------------------------
// Two arrows overlapping at 90°:
//   Arrow 1 — rotated −45°, translated (+6.2, −21.8)  → points upper-right
//   Arrow 2 — rotated 135°, translated (−6.2, +21.8)  → points lower-left
//
// Draw order (matches the React version):
//   Layer 1: thick background strokes (J-curve taper + inner-wing clip)
//   Layer 2: Arrow 2 white fill + thin outlines
//   Layer 3: Arrow 1 white fill + thin outlines  (on top → crossing effect)
function createArrowSymbol(size, strokeWidth = 5) {
  const clipId  = uid("inner-wing-clip");
  const clip2Id = uid("arrowhead-tip-clip");

  const a1x =  6.2,  a1y = -21.8;
  const a2x = -6.2,  a2y =  21.8;

  // ── defs: two clip-paths ──────────────────────────────────────────────────

  // Arrow 1 inner-wing clip:
  //   Restricts the thick inner-wing stroke to the valid concave region,
  //   bounded by the two diagonal slant-edge lines meeting at the tip (40,0).
  const cp1 = svgEl("clipPath", { id: clipId });
  cp1.appendChild(svgEl("polygon", { points: "-100,-100 8,-24 40,0 8,24 -100,100" }));

  // Arrow 2 arrowhead-tip clip:
  //   Clamps both the thick and thin slant-edge strokes to x ≥ 8 and y ∈ [−24,24]
  //   so their ends don't bleed past the inner-wing boundary.
  const cp2 = svgEl("clipPath", { id: clip2Id });
  cp2.appendChild(svgEl("polygon", { points: "8,-24 100,-24 100,24 8,24" }));

  const defs = svgEl("defs");
  defs.appendChild(cp1);
  defs.appendChild(cp2);

  // ── Layer 1a: Arrow 2 thick arrowhead tip ─────────────────────────────────
  const layer1a = svgEl("g", { transform: `translate(${a2x}, ${a2y}) rotate(135)` });
  layer1a.appendChild(svgEl("path", {
    d: ARROWHEAD_TIP_PATH,
    fill: "none", stroke: "black",
    "stroke-width": 9.5,
    "stroke-linejoin": "round",
    "stroke-linecap": "butt",
    "clip-path": `url(#${clip2Id})`,
  }));

  // ── Layer 1b: Arrow 1 tapered J-curve + thick inner wings ─────────────────
  const layer1b = svgEl("g", { transform: `translate(${a1x}, ${a1y}) rotate(-45)` });
  layer1b.appendChild(createTaperedJCurve(12));
  layer1b.appendChild(svgEl("path", {
    d: INNER_WINGS_PATH,
    fill: "none", stroke: "black",
    "stroke-width": 9.5,
    "stroke-linejoin": "round",
    "stroke-linecap": "butt",
    "clip-path": `url(#${clipId})`,
  }));

  // ── Layer 2: Arrow 2 white fill + per-segment thin outlines ───────────────
  const layer2 = svgEl("g", { transform: `translate(${a2x}, ${a2y}) rotate(135)` });
  layer2.appendChild(svgEl("path", { d: ARROW_PATH, fill: "white", stroke: "none" }));
  layer2.appendChild(svgEl("path", {
    d: "M -36,11 L 8,11",
    fill: "none", stroke: "black",
    "stroke-width": strokeWidth,
    "stroke-linecap": "round",
  }));
  layer2.appendChild(svgEl("path", {
    d: ARROWHEAD_TIP_PATH,
    fill: "none", stroke: "black",
    "stroke-width": strokeWidth,
    "stroke-linejoin": "round",
    "stroke-linecap": "butt",
    "clip-path": `url(#${clip2Id})`,
  }));
  layer2.appendChild(svgEl("path", {
    d: INNER_WINGS_PATH,
    fill: "none", stroke: "black",
    "stroke-width": strokeWidth * 0.714,
    "stroke-linejoin": "round",
    "stroke-linecap": "butt",
  }));
  layer2.appendChild(svgEl("path", {
    d: JCURVE_PATH,
    fill: "none", stroke: "black",
    "stroke-width": strokeWidth * 0.714,
    "stroke-linecap": "round",
  }));

  // ── Layer 3: Arrow 1 white fill + per-segment thin outlines ───────────────
  const layer3 = svgEl("g", { transform: `translate(${a1x}, ${a1y}) rotate(-45)` });
  layer3.appendChild(svgEl("path", { d: ARROW_PATH, fill: "white", stroke: "none" }));
  layer3.appendChild(svgEl("path", {
    d: "M -36,11 L 8,11",
    fill: "none", stroke: "black",
    "stroke-width": strokeWidth,
    "stroke-linecap": "round",
  }));
  layer3.appendChild(svgEl("path", {
    d: INNER_WINGS_PATH,
    fill: "none", stroke: "black",
    "stroke-width": strokeWidth,
    "stroke-linejoin": "round",
    "stroke-linecap": "butt",
  }));
  layer3.appendChild(svgEl("path", {
    d: JCURVE_PATH,
    fill: "none", stroke: "black",
    "stroke-width": strokeWidth,
    "stroke-linecap": "round",
  }));
  layer3.appendChild(svgEl("path", {
    d: ARROWHEAD_TIP_PATH,
    fill: "none", stroke: "black",
    "stroke-width": strokeWidth * 0.714,
    "stroke-linejoin": "miter",
    "stroke-linecap": "butt",
  }));

  // ── Assemble SVG ──────────────────────────────────────────────────────────
  const svg = svgEl("svg", {
    width: size, height: size,
    viewBox: "-72 -72 144 144",
    overflow: "visible",
  });
  svg.appendChild(defs);
  svg.appendChild(layer1a);
  svg.appendChild(layer1b);
  svg.appendChild(layer2);
  svg.appendChild(layer3);
  return svg;
}

// ---------------------------------------------------------------------------
// Card element
// ---------------------------------------------------------------------------
function createCard(color) {
  const bg = CARD_COLORS[color];
  const W = 210, H = 336;
  const ovalCX = W / 2, ovalCY = H / 2;

  // Card shell
  const card = document.createElement("div");
  card.className = "uno-card";
  card.style.width  = W + "px";
  card.style.height = H + "px";
  card.style.backgroundColor = bg;

  // Inner white frame
  const frame = document.createElement("div");
  frame.className = "card-frame";
  card.appendChild(frame);

  // Tilted oval (SVG layer clipped to card bounds via overflow:hidden on card)
  const ovalSvg = svgEl("svg", {
    class: "card-oval",
    width: W, height: H,
    viewBox: `0 0 ${W} ${H}`,
  });
  ovalSvg.appendChild(svgEl("ellipse", {
    cx: ovalCX, cy: ovalCY,
    rx: 76, ry: 155,
    fill: "none", stroke: "white", "stroke-width": 8,
    transform: `rotate(30, ${ovalCX}, ${ovalCY})`,
  }));
  card.appendChild(ovalSvg);

  // Centre double-arrow
  const centerWrap = document.createElement("div");
  centerWrap.className = "card-center";
  centerWrap.appendChild(createArrowSymbol(160, 3.5));
  card.appendChild(centerWrap);

  // Top-left corner arrow
  const tlWrap = document.createElement("div");
  tlWrap.className = "card-corner card-corner--tl";
  tlWrap.appendChild(createArrowSymbol(40, 3.5));
  card.appendChild(tlWrap);

  // Bottom-right corner arrow (rotated 180°)
  const brWrap = document.createElement("div");
  brWrap.className = "card-corner card-corner--br";
  brWrap.appendChild(createArrowSymbol(40, 3.5));
  card.appendChild(brWrap);

  return card;
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", function () {
  const container = document.getElementById("cards-container");
  ["red", "yellow", "blue", "green"].forEach(function (color) {
    container.appendChild(createCard(color));
  });
});
