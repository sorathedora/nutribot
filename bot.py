import os
import re
import json
import httpx
import asyncio
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ── Config ────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
ALLOWED_USER   = os.environ.get("ALLOWED_TELEGRAM_USER", "")
PORT           = int(os.environ.get("PORT", 8080))

SUPABASE_API   = f"{SUPABASE_URL}/rest/v1/meals"
HEADERS        = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
TARGETS        = {"cal": 1850, "protein": 145, "carbs": 160, "fat": 55, "fiber": 30}

# In-memory store for pending meals (keyed by short ID)
PENDING = {}

# ── Health check server ───────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"NutriTrack bot is running.")
    def log_message(self, *args):
        pass

def run_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()

# ── Helpers ───────────────────────────────────────────────────
def ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))

def today_str():
    now = ist_now()
    if now.hour < 4:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")

def now_time():
    return ist_now().strftime("%H:%M")

def parse_time_from_text(text: str):
    """Extract time and optional explicit date from text.
    Supported: 'at 1:30pm', 'at 13:30', '@1:30pm'
    Date modifiers (after the time): 'yesterday', 'DD Month', 'Month DD', 'YYYY-MM-DD'
    Returns (cleaned_text, HH:MM_str_or_None, date_str_or_None).
    """
    time_pat = r'(?:at|@)\s*(\d{1,2}):(\d{2})\s*(am|pm)?'
    m = re.search(time_pat, text, re.IGNORECASE)
    if not m:
        return text, None, None

    hh, mm = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hh != 12: hh += 12
    elif ampm == "am" and hh == 12: hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return text, None, None

    time_str = f"{hh:02d}:{mm:02d}"
    after = text[m.end():].strip()
    now = ist_now()

    # Try to parse an explicit date from the text immediately after the time
    date_str = None
    date_consumed = 0

    MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
              "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}

    if re.match(r'yesterday\b', after, re.IGNORECASE):
        date_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        date_consumed = len(re.match(r'yesterday\b', after, re.IGNORECASE).group())
    elif re.match(r'(\d{4})-(\d{2})-(\d{2})\b', after):
        dm = re.match(r'(\d{4})-(\d{2})-(\d{2})\b', after)
        date_str = dm.group()
        date_consumed = len(dm.group())
    elif re.match(r'(\d{1,2})\s+([a-z]{3,9})\b', after, re.IGNORECASE):
        dm = re.match(r'(\d{1,2})\s+([a-z]{3,9})\b', after, re.IGNORECASE)
        mon = MONTHS.get(dm.group(2).lower()[:3])
        if mon:
            date_str = now.replace(month=mon, day=int(dm.group(1))).strftime("%Y-%m-%d")
            date_consumed = len(dm.group())
    elif re.match(r'([a-z]{3,9})\s+(\d{1,2})\b', after, re.IGNORECASE):
        dm = re.match(r'([a-z]{3,9})\s+(\d{1,2})\b', after, re.IGNORECASE)
        mon = MONTHS.get(dm.group(1).lower()[:3])
        if mon:
            date_str = now.replace(month=mon, day=int(dm.group(2))).strftime("%Y-%m-%d")
            date_consumed = len(dm.group())

    remainder = after[date_consumed:].strip()
    cleaned = (text[:m.start()] + " " + remainder).strip()
    return cleaned, time_str, date_str or today_str()

async def db_insert(meal: dict):
    async with httpx.AsyncClient() as c:
        r = await c.post(SUPABASE_API, headers={**HEADERS, "Prefer": "return=representation"}, json=meal, timeout=10)
        r.raise_for_status()
        return r.json()

async def db_fetch_today():
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{SUPABASE_API}?meal_date=eq.{today_str()}&order=logged_at.asc", headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()

async def db_delete(meal_id: str):
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{SUPABASE_API}?id=eq.{meal_id}", headers={**HEADERS, "Prefer": "return=minimal"}, timeout=10)
        r.raise_for_status()

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER:
        return True
    return update.effective_user.username == ALLOWED_USER

def pct_bar(val, target, length=10):
    filled = min(int((val / target) * length), length)
    return "▓" * filled + "░" * (length - filled)

def format_today(meals: list) -> str:
    if not meals:
        return "Nothing logged today yet.\n\nSend a meal like:\n`chicken rice 400 35 45 8 2`\n_(name cal protein carbs fat fiber)_"
    totals = {k: 0 for k in TARGETS}
    lines = [f"📋 *Today — {today_str()}*\n"]
    for m in meals:
        lines.append(f"• *{m['name']}* `{m['meal_time']}`")
        lines.append(f"  {round(m['cal'])}kcal · {round(m['protein'])}p · {round(m['carbs'])}c · {round(m['fat'])}f")
        for k in totals:
            totals[k] += float(m.get(k) or 0)
    lines.append("\n📊 *Progress:*")
    for k, label, unit in [("cal","Calories","kcal"),("protein","Protein","g"),("carbs","Carbs","g"),("fat","Fat","g"),("fiber","Fiber","g")]:
        v, t = round(totals[k]), TARGETS[k]
        lines.append(f"`{label:<8}` {pct_bar(v,t)} {min(round((v/t)*100),100)}%  {v}/{t}{unit}")
    lines.append("\n🎯 *Remaining:*")
    for k, label, unit in [("cal","Cal","kcal"),("protein","Pro","g"),("carbs","Carbs","g"),("fat","Fat","g")]:
        rem = max(0, TARGETS[k] - totals[k])
        lines.append(f"{'✅' if rem==0 else '·'} {label}: {round(rem)}{unit}")
    return "\n".join(lines)

def parse_message(text: str):
    text = text.strip()
    text, custom_time, custom_date = parse_time_from_text(text)
    meal_time = custom_time or now_time()
    meal_date = custom_date or today_str()
    tagged = re.findall(r'(\d+\.?\d*)\s*(kcal|cal|p|pro|protein|c|carb|carbs|f|fat|fb|fiber|fibre)?', text, re.IGNORECASE)
    nums, plain = {}, []
    for val, tag in tagged:
        val = float(val)
        t = tag.lower() if tag else ""
        if t in ("kcal","cal"):           nums["cal"] = val
        elif t in ("p","pro","protein"):  nums["protein"] = val
        elif t in ("c","carb","carbs"):   nums["carbs"] = val
        elif t in ("f","fat"):            nums["fat"] = val
        elif t in ("fb","fiber","fibre"): nums["fiber"] = val
        elif not t:                       plain.append(val)
    for key in ["cal","protein","carbs","fat","fiber"]:
        if key not in nums and plain:
            nums[key] = plain.pop(0)
    if not nums or "cal" not in nums:
        return None, "❓ Couldn't find macros. Format:\n`meal name 320 38 12 12 2`\n_(name cal protein carbs fat fiber)_"
    name_part = re.sub(r'\b\d+\.?\d*\s*(kcal|cal|p|pro|protein|c|carb|carbs|f|fat|fb|fiber|fibre)?\b', '', text, flags=re.IGNORECASE)
    name = re.sub(r'\s+', ' ', name_part).strip().strip('-').strip() or "Meal"
    return {"meal_date": meal_date, "meal_time": meal_time, "name": name[:80],
            "cal": nums.get("cal",0), "protein": nums.get("protein",0),
            "carbs": nums.get("carbs",0), "fat": nums.get("fat",0),
            "fiber": nums.get("fiber",0)}, None

# ── Handlers ──────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "👋 *NutriTrack Bot*\n\n"
        "Log meals by sending:\n`meal name  cal  protein  carbs  fat  fiber`\n\n"
        "*Examples:*\n"
        "`chicken salad 320 38 12 12 2`\n"
        "`whey protein 131 30 1 0.6 0`\n"
        "`oats banana 380 12p 60c 6f 4fb`\n"
        "`chicken salad 320 38 12 12 at 1:30pm`\n"
        "`chicken salad 320 38 12 12 at 1:30pm yesterday`\n"
        "`chicken salad 320 38 12 12 at 1:30pm 31 May`\n"
        "`chicken salad 320 38 12 12 at 1:30pm 2026-05-31`\n\n"
        "Omitting a date logs to today.\n\n"
        "*/today* — log + progress\n"
        "*/undo* — remove last meal",
        parse_mode="Markdown"
    )

async def today_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        meals = await db_fetch_today()
        await update.message.reply_text(format_today(meals), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def undo_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        meals = await db_fetch_today()
        if not meals:
            await update.message.reply_text("Nothing to undo."); return
        last = meals[-1]
        await db_delete(last["id"])
        await update.message.reply_text(f"↩️ Removed: *{last['name']}*", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text.strip()
    if not text: return
    meal, err = parse_message(text)
    if err:
        await update.message.reply_text(err, parse_mode="Markdown"); return

    # Store meal in memory, pass only short ID in callback_data
    pending_id = str(uuid.uuid4())[:8]
    PENDING[pending_id] = meal

    is_yesterday = meal['meal_date'] != today_str()
    date_label = f"{meal['meal_date']} (yesterday)" if is_yesterday else "today"
    summary = (
        f"*{meal['name']}*\n"
        f"`{round(meal['cal'])} kcal  ·  {round(meal['protein'])}g protein`\n"
        f"`{round(meal['carbs'])}g carbs  ·  {round(meal['fat'])}g fat  ·  {round(meal['fiber'])}g fiber`\n\n"
        f"Log at *{meal['meal_time']}* on {date_label}?"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Log it", callback_data=f"log:{pending_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{pending_id}")
    ]])
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=keyboard)

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, pending_id = query.data.split(":", 1)

    if action == "cancel":
        PENDING.pop(pending_id, None)
        await query.edit_message_text("❌ Cancelled.")
        return

    meal = PENDING.pop(pending_id, None)
    if not meal:
        await query.edit_message_text("⚠️ Session expired. Please send the meal again.")
        return

    try:
        await db_insert(meal)
        meals = await db_fetch_today()
        totals = {k: sum(float(m.get(k) or 0) for m in meals) for k in TARGETS}
        cal_pct = min(round((totals["cal"]/TARGETS["cal"])*100), 100)
        pro_pct = min(round((totals["protein"]/TARGETS["protein"])*100), 100)
        await query.edit_message_text(
            f"✅ Logged: *{meal['name']}*\n"
            f"`{round(meal['cal'])} kcal · {round(meal['protein'])}g protein`\n\n"
            f"Today: {round(totals['cal'])}/1850 kcal ({cal_pct}%) · {round(totals['protein'])}/145g pro ({pro_pct}%)",
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"⚠️ Failed to log: {e}")

# ── Main ──────────────────────────────────────────────────────
async def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    print(f"Health server on port {PORT}")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    await app.initialize()
    await app.start()
    print("NutriTrack bot running...")
    await app.updater.start_polling(drop_pending_updates=True)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
