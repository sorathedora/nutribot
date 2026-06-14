// NutriTrack Activity Widget — iOS (Scriptable)
// ──────────────────────────────────────────────────────────────
// Setup:
//   1. Install Scriptable (App Store, free)
//   2. Open Scriptable → tap + → paste this file → name it "NutriTrack Activity"
//   3. Long-press home screen → + → Scriptable → medium size
//   4. Long-press widget → Edit Widget → Script = NutriTrack Activity
//   5. Tap widget → opens your dashboard (Activity tab)
// ──────────────────────────────────────────────────────────────

const SUPABASE  = "https://uikwwjmzrrxdvwmyguwv.supabase.co/rest/v1";
const ANON_KEY  = "sb_publishable_w9mMEmA0mGbQRs7s8ng76w_FX8hqqPd";
const DASH_URL  = "https://nutritrack-srv.netlify.app"; // ← update to your Netlify URL

const ACT_MIN   = 150;   // kcal to count as valid activity for streak
const WEIGHT_KG = 74;

const HEX = {
  bg:      "#0f0f0d",
  surface: "#181816",
  text:    "#f0efe8",
  muted:   "#666660",
  dim:     "#2e2e2a",
  accent:  "#c8f060",
  track:   "#1e1e1b",
  act:     "#a78bfa",
  actDim:  "#3a2060",
  good:    "#8ec94a",
};

const ACTIVITY_ICONS = {
  walk: "🚶", jog: "🏃", run: "🏃", badminton: "🏸", basketball: "🏀",
  swimming: "🏊", cycling: "🚴", yoga: "🧘", gym: "🏋️",
  football: "⚽", cricket: "🏏", dancing: "💃", hiking: "🥾",
  sex: "🔥", tennis: "🎾", squash: "🎾", volleyball: "🏐",
  boxing: "🥊", skipping: "⏭️", stair: "🪜", pilates: "🤸", crossfit: "💪",
};
function actIcon(name){ return ACTIVITY_ICONS[name] || "🏃"; }

// ── Helpers ────────────────────────────────────────────────────
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

// ── Ring drawing ───────────────────────────────────────────────
function makeRingImage(ratio, hexColor, sizePx = 56, strokeW = 6) {
  const dc = new DrawContext();
  dc.size = new Size(sizePx, sizePx);
  dc.opaque = false;
  const cx = sizePx / 2, cy = sizePx / 2;
  const radius = cx - strokeW / 2 - 1;
  const TWO_PI = Math.PI * 2;
  const START  = -Math.PI / 2;
  const STEPS  = 100;

  function arcPath(startFrac, endFrac) {
    const path = new Path();
    const span = Math.abs(endFrac - startFrac);
    const steps = Math.max(Math.round(STEPS * span), 4);
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const a = START + TWO_PI * (startFrac + (endFrac - startFrac) * t);
      const pt = new Point(cx + radius * Math.cos(a), cy + radius * Math.sin(a));
      i === 0 ? path.move(pt) : path.addLine(pt);
    }
    return path;
  }

  // Track
  dc.addPath(arcPath(0, 1));
  dc.setStrokeColor(new Color(HEX.track));
  dc.setLineWidth(strokeW);
  dc.strokePath();

  if (ratio <= 0) return dc.getImage();

  const isOver = ratio > 1;
  const mainFrac = Math.min(ratio, 1);

  // Main arc
  dc.addPath(arcPath(0, mainFrac));
  dc.setStrokeColor(new Color(isOver ? "#ff9f43" : hexColor));
  dc.setLineWidth(strokeW);
  dc.strokePath();

  // Overshoot wrap
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

// ── Fetch data ─────────────────────────────────────────────────
const today = todayStr();
let todayActs = [], actStreak = 0, actFreezeBal = 0, nutStreak = 0;

try { todayActs = await supaGet(`/activities?activity_date=eq.${today}&order=logged_at.asc`); } catch (_) {}

try {
  const rows = await supaGet("/settings?key=eq.act_freeze_balance");
  actFreezeBal = parseInt(rows[0]?.value || "0");
} catch (_) {}

// Compute activity streak from last 30 days
try {
  const todayDt = new Date(today + "T12:00:00");
  const from    = new Date(todayDt); from.setDate(from.getDate() - 29);
  const fromStr = from.toISOString().split("T")[0];

  const hist = await supaGet(
    `/activities?activity_date=gte.${fromStr}&activity_date=lte.${today}&order=activity_date.asc`
  );
  const actByDate = {};
  hist.forEach(a => { (actByDate[a.activity_date] = actByDate[a.activity_date] || []).push(a); });

  const isValid = k => (actByDate[k] || []).some(a => (a.calories_burned || 0) >= ACT_MIN);
  const startI = isValid(today) ? 0 : 1;
  for (let i = startI; i < 30; i++) {
    const d = new Date(todayDt); d.setDate(d.getDate() - i);
    const k = d.toISOString().split("T")[0];
    if (isValid(k)) actStreak++; else break;
  }
} catch (_) {}

// Nutrition streak (optional — may fail gracefully if no meals table access)
try {
  const nutRows = await supaGet("/settings?key=eq.streak_milestone_credited");
  // Just fetch the streak from meals for last 30 days
  const todayDt = new Date(today + "T12:00:00");
  const from    = new Date(todayDt); from.setDate(from.getDate() - 29);
  const fromStr = from.toISOString().split("T")[0];
  const meals   = await supaGet(`/meals?meal_date=gte.${fromStr}&meal_date=lte.${today}&order=meal_date.asc`);
  const mByDate = {};
  meals.forEach(m => { (mByDate[m.meal_date] = mByDate[m.meal_date] || []).push(m); });
  const isGood = k => { const ms = mByDate[k] || []; return ms.length >= 2 && ms.reduce((s,m)=>s+(m.protein||0),0) >= 87; };
  const startI = isGood(today) ? 0 : 1;
  for (let i = startI; i < 30; i++) {
    const d = new Date(todayDt); d.setDate(d.getDate() - i);
    const k = d.toISOString().split("T")[0];
    if (isGood(k)) nutStreak++; else break;
  }
} catch (_) {}

// ── Totals ─────────────────────────────────────────────────────
const totalBurned = todayActs.reduce((s, a) => s + (a.calories_burned || 0), 0);
const validCount  = todayActs.filter(a => (a.calories_burned || 0) >= ACT_MIN).length;

// ── Widget layout ──────────────────────────────────────────────
const widget = new ListWidget();
widget.backgroundColor = new Color(HEX.bg);
widget.url = DASH_URL;
widget.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
widget.setPadding(12, 14, 11, 14);

// ── Top row: activity ring + streaks ──────────────────────────
const topRow = widget.addStack();
topRow.layoutHorizontally();
topRow.centerAlignContent();
topRow.spacing = 0;

// Activity ring (calories burned vs goal of 300)
const ringCell = topRow.addStack();
ringCell.layoutVertically();
ringCell.centerAlignContent();

const actRatio   = totalBurned / 300;   // goal = 300 kcal
const ringImg    = ringCell.addImage(makeRingImage(actRatio, HEX.act, 72, 7));
ringImg.imageSize = new Size(72, 72);
ringImg.centerAlignImage();

const burnPct = ringCell.addText(Math.round(actRatio * 100) + "%");
burnPct.font = Font.boldSystemFont(11);
burnPct.textColor = new Color(actRatio >= 1 ? "#ff9f43" : HEX.act);
burnPct.centerAlignText();

const burnLbl = ringCell.addText("Burned");
burnLbl.font = Font.systemFont(10);
burnLbl.textColor = new Color(HEX.muted);
burnLbl.centerAlignText();

topRow.addSpacer();

// Streak + stats column
const statsCol = topRow.addStack();
statsCol.layoutVertically();
statsCol.spacing = 6;

// Activity streak
const actStreakRow = statsCol.addStack();
actStreakRow.layoutHorizontally();
actStreakRow.spacing = 4;
const actStrTxt = actStreakRow.addText("🏃 " + actStreak);
actStrTxt.font = Font.boldSystemFont(16);
actStrTxt.textColor = new Color(actStreak > 0 ? HEX.act : HEX.muted);
const actStreakLabel = actStreakRow.addText(" day streak");
actStreakLabel.font = Font.systemFont(12);
actStreakLabel.textColor = new Color(HEX.muted);

// Nutrition streak
const nutStreakRow = statsCol.addStack();
nutStreakRow.layoutHorizontally();
nutStreakRow.spacing = 4;
const nutStrTxt = nutStreakRow.addText("🥗 " + nutStreak);
nutStrTxt.font = Font.boldSystemFont(16);
nutStrTxt.textColor = new Color(nutStreak > 0 ? HEX.good : HEX.muted);
const nutStreakLabel = nutStreakRow.addText(" day streak");
nutStreakLabel.font = Font.systemFont(12);
nutStreakLabel.textColor = new Color(HEX.muted);

// Calories burned + freeze balance
const metaRow = statsCol.addStack();
metaRow.layoutHorizontally();
metaRow.spacing = 6;
const burnedTxt = metaRow.addText(totalBurned + " kcal");
burnedTxt.font = Font.systemFont(11);
burnedTxt.textColor = new Color(HEX.muted);
if (actFreezeBal > 0) {
  const freeTxt = metaRow.addText("❄️ " + actFreezeBal);
  freeTxt.font = Font.systemFont(11);
  freeTxt.textColor = new Color("#9a80d0");
}

widget.addSpacer(8);

// ── Activity list ──────────────────────────────────────────────
if (todayActs.length === 0) {
  const noActs = widget.addText("No activities logged today");
  noActs.font = Font.systemFont(11);
  noActs.textColor = new Color(HEX.muted);
} else {
  // Show up to 3 activities
  const showActs = todayActs.slice(0, 3);
  for (const a of showActs) {
    const row = widget.addStack();
    row.layoutHorizontally();
    row.spacing = 0;

    const icon = row.addText(actIcon(a.activity) + " ");
    icon.font = Font.systemFont(12);
    icon.textColor = new Color(HEX.text);

    const nameTxt = row.addText(a.activity.charAt(0).toUpperCase() + a.activity.slice(1));
    nameTxt.font = Font.systemFont(12);
    nameTxt.textColor = new Color(HEX.text);

    row.addSpacer();

    const valid = (a.calories_burned || 0) >= ACT_MIN;
    const calTxt = row.addText(a.calories_burned + " kcal" + (valid ? " ✓" : ""));
    calTxt.font = Font.systemFont(11);
    calTxt.textColor = new Color(valid ? HEX.act : HEX.muted);

    widget.addSpacer(2);
  }
  if (todayActs.length > 3) {
    const moreTxt = widget.addText("+" + (todayActs.length - 3) + " more");
    moreTxt.font = Font.systemFont(10);
    moreTxt.textColor = new Color(HEX.muted);
  }
}

widget.addSpacer();

// ── Bottom bar ─────────────────────────────────────────────────
const infoRow = widget.addStack();
infoRow.layoutHorizontally();
infoRow.centerAlignContent();

const brand = infoRow.addText("NutriTrack");
brand.font = Font.boldSystemFont(10);
brand.textColor = new Color(HEX.accent);

infoRow.addSpacer();

const dateLabel = new Date(today + "T12:00:00")
  .toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
const dateTxt = infoRow.addText(dateLabel);
dateTxt.font = Font.systemFont(10);
dateTxt.textColor = new Color(HEX.muted);

infoRow.addSpacer();

const validTxt = infoRow.addText(validCount > 0 ? `${validCount} valid ✓` : "no valid activity");
validTxt.font = Font.systemFont(10);
validTxt.textColor = new Color(validCount > 0 ? HEX.act : "#444440");

Script.setWidget(widget);
Script.complete();
