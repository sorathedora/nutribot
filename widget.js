// NutriTrack — iOS Widget (Scriptable)
// ──────────────────────────────────────────────────────────────
// Setup:
//   1. Install Scriptable (App Store, free)
//   2. Open Scriptable → tap + → paste this whole file → name it "NutriTrack"
//   3. Long-press home screen → + → Scriptable → pick medium size
//   4. Long-press the widget → Edit Widget → Script = NutriTrack
//   5. Tap widget → opens your dashboard
// ──────────────────────────────────────────────────────────────

const SUPABASE = "https://uikwwjmzrrxdvwmyguwv.supabase.co/rest/v1";
const ANON_KEY = "sb_publishable_w9mMEmA0mGbQRs7s8ng76w_FX8hqqPd";
const DASH_URL = "https://chimerical-nougat-d292f9.netlify.app/"; // ← update to your Netlify URL

const TARGETS = { cal: 1850, protein: 145, carbs: 160, fat: 55 };

// Exact same palette as the web dashboard
const HEX = {
  bg:      "#0f0f0d",
  surface: "#181816",
  text:    "#f0efe8",
  muted:   "#666660",
  dim:     "#2e2e2a",
  accent:  "#c8f060",
  track:   "#1e1e1b",
  over:    "#ff9f43",
  cal:     "#7ab8f5",
  protein: "#8ec94a",
  carbs:   "#e8a84a",
  fat:     "#d97aaa",
};

// ── Helpers ───────────────────────────────────────────────────
function todayStr() {
  const ist = new Date(Date.now() + 5.5 * 3600000);
  if (ist.getUTCHours() < 4) ist.setUTCDate(ist.getUTCDate() - 1);
  return ist.toISOString().split("T")[0];
}

async function supaGet(path) {
  const req = new Request(SUPABASE + path);
  req.headers = { apikey: ANON_KEY, Authorization: "Bearer " + ANON_KEY };
  return req.loadJSON();
}

// ── Ring drawing ──────────────────────────────────────────────
// Draws a single ring as a DrawContext image.
// Handles overshoot: full ring goes orange, then colour arc wraps on top.
function makeRingImage(ratio, hexColor, sizePx = 64, strokeW = 7) {
  const dc = new DrawContext();
  dc.size = new Size(sizePx, sizePx);
  dc.opaque = false;

  const cx = sizePx / 2;
  const cy = sizePx / 2;
  const radius = cx - strokeW / 2 - 1;
  const TWO_PI = Math.PI * 2;
  const START  = -Math.PI / 2; // 12 o'clock
  const STEPS  = 120;          // arc smoothness

  // Build a Path following a partial or full arc
  function arcPath(startFrac, endFrac) {
    const path  = new Path();
    const span  = Math.abs(endFrac - startFrac);
    const steps = Math.max(Math.round(STEPS * span), 4);
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const a = START + TWO_PI * (startFrac + (endFrac - startFrac) * t);
      const pt = new Point(cx + radius * Math.cos(a), cy + radius * Math.sin(a));
      i === 0 ? path.move(pt) : path.addLine(pt);
    }
    return path;
  }

  // 1. Track (full circle, darkest)
  dc.addPath(arcPath(0, 1));
  dc.setStrokeColor(new Color(HEX.track));
  dc.setLineWidth(strokeW);
  dc.strokePath();

  if (ratio <= 0) return dc.getImage();

  const isOver   = ratio > 1;
  const mainFrac = Math.min(ratio, 1);

  // 2. Main arc — colour up to 100%, orange if over
  dc.addPath(arcPath(0, mainFrac));
  dc.setStrokeColor(new Color(isOver ? HEX.over : hexColor));
  dc.setLineWidth(strokeW);
  dc.strokePath();

  // 3. Overshoot wrap — coloured arc starting from 12 o'clock again
  if (isOver) {
    const overFrac = Math.min(ratio - 1, 1);
    if (overFrac > 0) {
      dc.addPath(arcPath(0, overFrac));
      dc.setStrokeColor(new Color(hexColor));
      dc.setLineWidth(strokeW);
      dc.strokePath();
    }
  }

  return dc.getImage();
}

// ── Fetch data ────────────────────────────────────────────────
const today = todayStr();
let meals = [], freezeBal = 0, streak = 0;

try { meals = await supaGet(`/meals?meal_date=eq.${today}&order=logged_at.asc`); } catch (_) {}

try {
  const rows = await supaGet("/settings?key=eq.freeze_balance");
  freezeBal = parseInt(rows[0]?.value || "0");
} catch (_) {}

try {
  const todayDt = new Date(today + "T12:00:00");
  const from    = new Date(todayDt);
  from.setDate(from.getDate() - 29);
  const fromStr = from.toISOString().split("T")[0];

  const hist = await supaGet(
    `/meals?meal_date=gte.${fromStr}&meal_date=lte.${today}&order=meal_date.asc`
  );
  const byDate = {};
  hist.forEach(m => { (byDate[m.meal_date] = byDate[m.meal_date] || []).push(m); });

  const cheatSet = new Set();
  try {
    const cd = await supaGet("/cheat_days?order=date.desc");
    cd.forEach(x => cheatSet.add(x.date));
  } catch (_) {}

  const isGoodDay = k => {
    if (cheatSet.has(k)) return true;
    const ms = byDate[k] || [];
    return ms.length >= 2 && ms.reduce((s, m) => s + (m.protein || 0), 0) >= 87;
  };

  const startI = isGoodDay(today) ? 0 : 1;
  for (let i = startI; i < 30; i++) {
    const d = new Date(todayDt);
    d.setDate(d.getDate() - i);
    const k = d.toISOString().split("T")[0];
    if (isGoodDay(k)) streak++; else break;
  }
} catch (_) {}

// Totals
const T = meals.reduce(
  (a, m) => ({
    cal:     a.cal     + (m.cal     || 0),
    protein: a.protein + (m.protein || 0),
    carbs:   a.carbs   + (m.carbs   || 0),
    fat:     a.fat     + (m.fat     || 0),
  }),
  { cal: 0, protein: 0, carbs: 0, fat: 0 }
);

// ── Widget layout ─────────────────────────────────────────────
const RING_SIZE = 64; // px — roughly app-icon scale for medium widget
const STROKE    = 7;

const widget = new ListWidget();
widget.backgroundColor = new Color(HEX.bg);
widget.url = DASH_URL;
widget.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
widget.setPadding(12, 14, 11, 14);

// ── Top: four rings, evenly spaced ───────────────────────────
const ringsRow = widget.addStack();
ringsRow.layoutHorizontally();
ringsRow.centerAlignContent();

const macros = [
  { key: "cal",     label: "Cal",    color: HEX.cal,     unit: "kcal" },
  { key: "protein", label: "Pro",    color: HEX.protein, unit: "g"    },
  { key: "carbs",   label: "Carbs",  color: HEX.carbs,   unit: "g"    },
  { key: "fat",     label: "Fat",    color: HEX.fat,     unit: "g"    },
];

macros.forEach(({ key, label, color, unit }, idx) => {
  // Flexible spacer between rings (and at the edges via padding)
  if (idx > 0) ringsRow.addSpacer();

  const val   = Math.round(T[key]);
  const tgt   = TARGETS[key];
  const ratio = val / tgt;
  const isOver = ratio > 1;
  const pctDisplay = Math.round(ratio * 100) + "%";

  // Cell: ring image + % text + label
  const cell = ringsRow.addStack();
  cell.layoutVertically();
  cell.centerAlignContent();
  cell.spacing = 3;

  const img = cell.addImage(makeRingImage(ratio, color, RING_SIZE, STROKE));
  img.imageSize = new Size(RING_SIZE, RING_SIZE);
  img.centerAlignImage();

  const pctTxt = cell.addText(pctDisplay);
  pctTxt.font = Font.boldSystemFont(11);
  pctTxt.textColor = new Color(isOver ? HEX.over : color);
  pctTxt.centerAlignText();

  const lblTxt = cell.addText(label);
  lblTxt.font = Font.systemFont(10);
  lblTxt.textColor = new Color(HEX.muted);
  lblTxt.centerAlignText();
});

widget.addSpacer();

// ── Bottom: brand + date + streak + meals ────────────────────
const infoRow = widget.addStack();
infoRow.layoutHorizontally();
infoRow.centerAlignContent();
infoRow.spacing = 0;

// Brand name
const brand = infoRow.addText("NutriTrack");
brand.font = Font.boldSystemFont(11);
brand.textColor = new Color(HEX.accent);

infoRow.addSpacer();

// Date
const dateLabel = new Date(today + "T12:00:00")
  .toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
const dateTxt = infoRow.addText(dateLabel);
dateTxt.font = Font.systemFont(10);
dateTxt.textColor = new Color(HEX.muted);

infoRow.addSpacer();

// Streak / freeze / meal count
const meta = [];
if (streak > 0) meta.push(`🔥 ${streak}`);
if (freezeBal > 0) meta.push(`❄️ ${freezeBal}`);
meta.push(`${meals.length} meal${meals.length !== 1 ? "s" : ""}`);
const metaTxt = infoRow.addText(meta.join("  "));
metaTxt.font = Font.systemFont(10);
metaTxt.textColor = new Color("#555550");

Script.setWidget(widget);
Script.complete();
