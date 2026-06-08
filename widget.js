// NutriTrack — iOS Widget (Scriptable)
// ─────────────────────────────────────
// Setup:
//   1. Install Scriptable from the App Store (free)
//   2. Open Scriptable → tap + → paste this entire file → name it "NutriTrack"
//   3. Long-press home screen → add widget → Scriptable → medium size
//   4. Edit the widget → set Script = NutriTrack
//   5. Tap the widget to open your dashboard
// ─────────────────────────────────────

const SUPABASE  = "https://uikwwjmzrrxdvwmyguwv.supabase.co/rest/v1";
const ANON_KEY  = "sb_publishable_w9mMEmA0mGbQRs7s8ng76w_FX8hqqPd";
const DASH_URL  = "https://nutritrack-srv.netlify.app"; // ← your Netlify URL
const TARGETS   = { cal:1850, protein:145, carbs:160, fat:55 };

// ── Colors ───────────────────────────
const C = {
  bg:      new Color("#0f0f0d"),
  surface: new Color("#181816"),
  text:    new Color("#f0efe8"),
  muted:   new Color("#666660"),
  dim:     new Color("#3a3a36"),
  accent:  new Color("#c8f060"),
  over:    new Color("#ff9f43"),
  track:   new Color("#2a2a26"),
};
const MACRO_COLOR = {
  cal:     new Color("#7ab8f5"),
  protein: new Color("#8ec94a"),
  carbs:   new Color("#e8a84a"),
  fat:     new Color("#d97aaa"),
};

// ── Helpers ──────────────────────────
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

// Draw a rounded progress bar as an image.
// Handles overshoot: fills full bar then wraps a second arc on top in full color.
function makeBar(pct, color, w = 100, h = 5) {
  const dc = new DrawContext();
  dc.size = new Size(w, h);
  dc.opaque = false;
  const r = h / 2;

  // Track
  dc.setFillColor(C.track);
  const track = new Path();
  track.addRoundedRect(new Rect(0, 0, w, h), r, r);
  dc.addPath(track);
  dc.fillPath();

  // Fill — normal up to 100%, orange if over
  const fillW = Math.min(pct, 1) * w;
  dc.setFillColor(pct > 1 ? C.over : color);
  const fill = new Path();
  fill.addRoundedRect(new Rect(0, 0, fillW, h), r, r);
  dc.addPath(fill);
  dc.fillPath();

  // Overshoot wrap-around (Apple Fitness style)
  if (pct > 1) {
    const overW = Math.min((pct - 1) * w, w * 0.8);
    dc.setFillColor(color);
    const over = new Path();
    over.addRoundedRect(new Rect(0, 0, overW, h), r, r);
    dc.addPath(over);
    dc.fillPath();
  }

  return dc.getImage();
}

function pct(val, tgt) { return Math.round((val / tgt) * 100); }

// ── Fetch data ────────────────────────
const today = todayStr();
let meals = [], freezeBal = 0, streak = 0;

try {
  meals = await supaGet(`/meals?meal_date=eq.${today}&order=logged_at.asc`);
} catch (_) {}

try {
  const fb = await supaGet("/settings?key=eq.freeze_balance");
  freezeBal = parseInt(fb[0]?.value || "0");
} catch (_) {}

// Simple 30-day streak calc (good day = ≥2 meals + ≥87g protein)
try {
  const todayDt = new Date(today + "T12:00:00");
  const from = new Date(todayDt); from.setDate(from.getDate() - 29);
  const fromStr = from.toISOString().split("T")[0];
  const hist = await supaGet(`/meals?meal_date=gte.${fromStr}&meal_date=lte.${today}&order=meal_date.asc`);
  const byDate = {};
  hist.forEach(m => { (byDate[m.meal_date] = byDate[m.meal_date] || []).push(m); });
  const cheatData = await supaGet("/cheat_days?order=date.desc");
  const cheatDays = new Set(cheatData.map(x => x.date));
  const isGood = k => {
    if (cheatDays.has(k)) return true;
    const ms = byDate[k] || [];
    return ms.length >= 2 && ms.reduce((s, m) => s + (m.protein || 0), 0) >= 87;
  };
  const startI = isGood(today) ? 0 : 1;
  for (let i = startI; i < 30; i++) {
    const d = new Date(todayDt); d.setDate(d.getDate() - i);
    const k = d.toISOString().split("T")[0];
    if (isGood(k)) streak++; else break;
  }
} catch (_) {}

// ── Totals ────────────────────────────
const T = meals.reduce(
  (a, m) => ({ cal: a.cal+(m.cal||0), protein: a.protein+(m.protein||0),
                carbs: a.carbs+(m.carbs||0), fat: a.fat+(m.fat||0) }),
  { cal: 0, protein: 0, carbs: 0, fat: 0 }
);

// ── Build widget ──────────────────────
const isSmall = config.widgetFamily === "small";
const widget = new ListWidget();
widget.backgroundColor = C.bg;
widget.url = DASH_URL;
widget.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000); // hint: 15 min

widget.setPadding(14, 14, 12, 14);

// Header row
const hdr = widget.addStack();
hdr.layoutHorizontally();
hdr.centerAlignContent();

const titleTxt = hdr.addText("NutriTrack");
titleTxt.font = Font.boldSystemFont(13);
titleTxt.textColor = C.accent;
hdr.addSpacer();

const dateLabel = new Date(today + "T12:00:00")
  .toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
const dateTxt = hdr.addText(dateLabel);
dateTxt.font = Font.systemFont(11);
dateTxt.textColor = C.muted;

widget.addSpacer(8);

// Macro rows — show all 4 on medium, just cal+protein on small
const macros = isSmall
  ? [["cal","Cal"],["protein","Pro"]]
  : [["cal","Calories"],["protein","Protein"],["carbs","Carbs"],["fat","Fat"]];

for (const [key, label] of macros) {
  const val = Math.round(T[key]);
  const tgt = TARGETS[key];
  const ratio = val / tgt;
  const color = MACRO_COLOR[key];
  const isOver = ratio > 1;
  const unit = key === "cal" ? " kcal" : "g";

  const row = widget.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();

  // Label
  const lbl = row.addText(label);
  lbl.font = Font.systemFont(11);
  lbl.textColor = C.muted;
  lbl.lineLimit = 1;

  row.addSpacer(6);

  // Progress bar
  const barW = isSmall ? 70 : 88;
  const barImg = row.addImage(makeBar(ratio, color, barW, 5));
  barImg.imageSize = new Size(barW, 5);
  barImg.resizable = false;

  row.addSpacer(6);

  // Value
  const valTxt = row.addText(`${val}/${tgt}${unit}`);
  valTxt.font = Font.systemFont(11);
  valTxt.textColor = isOver ? C.over : color;
  valTxt.lineLimit = 1;
  valTxt.minimumScaleFactor = 0.7;

  widget.addSpacer(isSmall ? 6 : 5);
}

widget.addSpacer();

// Footer: streak + freeze + meal count
const ftr = widget.addStack();
ftr.layoutHorizontally();
ftr.centerAlignContent();

if (streak > 0) {
  const stk = ftr.addText(`🔥 ${streak}`);
  stk.font = Font.systemFont(10);
  stk.textColor = C.dim;
  ftr.addSpacer(6);
}

if (freezeBal > 0) {
  const frz = ftr.addText(`❄️ ${freezeBal}`);
  frz.font = Font.systemFont(10);
  frz.textColor = C.dim;
  ftr.addSpacer(6);
}

const mc = ftr.addText(`${meals.length} meal${meals.length !== 1 ? "s" : ""}`);
mc.font = Font.systemFont(10);
mc.textColor = C.dim;

ftr.addSpacer();

const tapHint = ftr.addText("tap →");
tapHint.font = Font.systemFont(10);
tapHint.textColor = new Color("#2a2a26");

Script.setWidget(widget);
Script.complete();
