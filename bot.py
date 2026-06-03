import os
import re
import time as _time
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
SELF_URL       = os.environ.get("RENDER_EXTERNAL_URL", "")   # set automatically by Render

SUPABASE_API   = f"{SUPABASE_URL}/rest/v1/meals"
PRESETS_API    = f"{SUPABASE_URL}/rest/v1/presets"
# NOTE: run this once in Supabase SQL editor to create the presets table:
#   CREATE TABLE presets (
#     id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
#     name text UNIQUE NOT NULL,
#     cal float NOT NULL DEFAULT 0, protein float NOT NULL DEFAULT 0,
#     carbs float NOT NULL DEFAULT 0, fat float NOT NULL DEFAULT 0,
#     fiber float NOT NULL DEFAULT 0,
#     created_at timestamptz DEFAULT now()
#   );
#   ALTER TABLE presets ENABLE ROW LEVEL SECURITY;
#   CREATE POLICY "anon_all" ON presets FOR ALL TO anon USING (true) WITH CHECK (true);
HEADERS        = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
TARGETS        = {"cal": 1850, "protein": 145, "carbs": 160, "fat": 55, "fiber": 30}

# ── State ─────────────────────────────────────────────────────
# PENDING format: {id: {"action": "log"|"copy", "data": meal_dict or [meal_dicts]}}
PENDING       = {}
REMINDER      = {}   # {"time": "HH:MM", "chat_id": int}
REMINDER_SENT = {}   # {"date": "YYYY-MM-DD"} — prevent duplicate sends per day
APP           = None # set in main(), used by reminder_loop

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

# ── Time helpers ──────────────────────────────────────────────
def ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))

def today_str():
    """Current date under the 4am IST day boundary."""
    now = ist_now()
    if now.hour < 4:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")

def yesterday_str():
    """Yesterday under the 4am IST day boundary."""
    now = ist_now()
    if now.hour < 4:
        now = now - timedelta(days=1)
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")

def now_time():
    return ist_now().strftime("%H:%M")

def fmt12(t: str) -> str:
    """Convert HH:MM to 12h format e.g. 1:30 PM."""
    try:
        hh, mm = map(int, t.split(":"))
        period = "AM" if hh < 12 else "PM"
        hh12 = hh % 12 or 12
        return f"{hh12}:{mm:02d} {period}"
    except Exception:
        return t

def human_date(date_str: str) -> str:
    """today / yesterday / Mon 02 Jun."""
    if date_str == today_str():     return "today"
    if date_str == yesterday_str(): return "yesterday"
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b")
    except Exception:
        return date_str

def parse_reminder_time(text: str):
    """Parse '9pm', '9:30pm', '21:00', '21:30' → 'HH:MM' or None."""
    m = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', text.strip(), re.IGNORECASE)
    if not m: return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hh != 12: hh += 12
    elif ampm == "am" and hh == 12: hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59): return None
    return f"{hh:02d}:{mm:02d}"

# ── Parsing ───────────────────────────────────────────────────
def parse_time_from_text(text: str):
    """Extract time and optional explicit date from text.
    Supported: 'at 1:30pm', 'at 13:30', '@1:30pm'
    Date modifiers: 'yesterday', 'DD Month', 'Month DD', 'YYYY-MM-DD'
    Returns (cleaned_text, HH:MM_or_None, date_str_or_None).
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

    date_str = None
    date_consumed = 0
    MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
              "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}

    if re.match(r'yesterday\b', after, re.IGNORECASE):
        date_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        date_consumed = len(re.match(r'yesterday\b', after, re.IGNORECASE).group())
    elif re.match(r'(\d{4})-(\d{2})-(\d{2})\b', after):
        dm = re.match(r'(\d{4})-(\d{2})-(\d{2})\b', after)
        date_str = dm.group(); date_consumed = len(dm.group())
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

    # Late night rule: 00:00–03:59 with no explicit date → previous day
    if not date_str and hh < 4:
        date_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    remainder = after[date_consumed:].strip()
    cleaned = (text[:m.start()] + " " + remainder).strip()
    return cleaned, time_str, date_str or today_str()

def parse_message(text: str):
    text = text.strip()
    text, custom_time, custom_date = parse_time_from_text(text)
    meal_time = custom_time or now_time()
    meal_date = custom_date or today_str()

    macro_tok = re.compile(r'^(\d+\.?\d*)(kcal|cal|p|pro|protein|c|carb|carbs|f|fat|fb|fiber|fibre)?$', re.IGNORECASE)
    tokens = text.split()
    macro_start = len(tokens)
    for i in range(len(tokens) - 1, -1, -1):
        if macro_tok.match(tokens[i]): macro_start = i
        else: break
    name = " ".join(tokens[:macro_start]).strip() or "Meal"
    macro_txt = " ".join(tokens[macro_start:])

    tagged = re.findall(r'(\d+\.?\d*)\s*(kcal|cal|p|pro|protein|c|carb|carbs|f|fat|fb|fiber|fibre)?', macro_txt, re.IGNORECASE)
    nums, plain = {}, []
    for val, tag in tagged:
        val = float(val); t = tag.lower() if tag else ""
        if t in ("kcal","cal"):           nums["cal"] = val
        elif t in ("p","pro","protein"):  nums["protein"] = val
        elif t in ("c","carb","carbs"):   nums["carbs"] = val
        elif t in ("f","fat"):            nums["fat"] = val
        elif t in ("fb","fiber","fibre"): nums["fiber"] = val
        elif not t:                       plain.append(val)
    for key in ["cal","protein","carbs","fat","fiber"]:
        if key not in nums and plain: nums[key] = plain.pop(0)
    if not nums or "cal" not in nums:
        return None, "❓ Couldn't find macros. Format:\n`meal name 320 38 12 12 2`\n_(name cal protein carbs fat fiber)_"
    return {"meal_date": meal_date, "meal_time": meal_time, "name": name[:80],
            "cal": nums.get("cal",0), "protein": nums.get("protein",0),
            "carbs": nums.get("carbs",0), "fat": nums.get("fat",0),
            "fiber": nums.get("fiber",0)}, None

# ── DB — meals ────────────────────────────────────────────────
async def db_insert(meal: dict):
    async with httpx.AsyncClient() as c:
        r = await c.post(SUPABASE_API, headers={**HEADERS, "Prefer": "return=representation"}, json=meal, timeout=10)
        r.raise_for_status(); return r.json()

async def db_fetch_date(date_str: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{SUPABASE_API}?meal_date=eq.{date_str}&order=logged_at.asc", headers=HEADERS, timeout=10)
        r.raise_for_status(); return r.json()

async def db_fetch_today():
    return await db_fetch_date(today_str())

async def db_fetch_range(from_date: str, to_date: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{SUPABASE_API}?meal_date=gte.{from_date}&meal_date=lte.{to_date}&order=meal_date.asc,logged_at.asc",
            headers=HEADERS, timeout=10)
        r.raise_for_status(); return r.json()

async def db_delete(meal_id: str):
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{SUPABASE_API}?id=eq.{meal_id}", headers={**HEADERS, "Prefer": "return=minimal"}, timeout=10)
        r.raise_for_status()

async def db_update(meal_id: str, updates: dict):
    async with httpx.AsyncClient() as c:
        r = await c.patch(
            f"{SUPABASE_API}?id=eq.{meal_id}",
            headers={**HEADERS, "Prefer": "return=representation"},
            json=updates, timeout=10)
        r.raise_for_status(); return r.json()

# ── DB — presets ──────────────────────────────────────────────
async def db_save_preset(name: str, macros: dict):
    async with httpx.AsyncClient() as c:
        r = await c.post(
            PRESETS_API,
            headers={**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
            json={"name": name.lower(), **macros}, timeout=10)
        r.raise_for_status(); return r.json()

async def db_list_presets():
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{PRESETS_API}?order=name.asc", headers=HEADERS, timeout=10)
        r.raise_for_status(); return r.json()

async def db_fetch_preset(name: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{PRESETS_API}?name=eq.{name.lower()}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json(); return data[0] if data else None

async def db_delete_preset(name: str):
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{PRESETS_API}?name=eq.{name.lower()}", headers={**HEADERS, "Prefer": "return=minimal"}, timeout=10)
        r.raise_for_status()

# ── Background tasks ──────────────────────────────────────────
async def keep_alive():
    """Ping own health endpoint every 10 min to prevent Render free tier sleep."""
    if not SELF_URL:
        print("SELF_URL not set — keep-alive disabled (set RENDER_EXTERNAL_URL env var)")
        return
    await asyncio.sleep(30)
    while True:
        try:
            async with httpx.AsyncClient() as c:
                await c.get(SELF_URL, timeout=15)
        except Exception:
            pass
        await asyncio.sleep(10 * 60)

async def reminder_loop():
    """Send a daily reminder if nothing logged by the set time."""
    while True:
        await asyncio.sleep(60)
        if not REMINDER or not APP:
            continue
        now = ist_now()
        now_hm = f"{now.hour:02d}:{now.minute:02d}"
        today = today_str()
        if now_hm == REMINDER["time"] and REMINDER_SENT.get("date") != today:
            REMINDER_SENT["date"] = today  # mark sent regardless so we don't loop
            try:
                meals = await db_fetch_today()
                if not meals:
                    await APP.bot.send_message(
                        REMINDER["chat_id"],
                        f"⏰ *Daily reminder* — nothing logged today yet!\n\n"
                        f"How's your nutrition going? Log a meal to start.",
                        parse_mode="Markdown"
                    )
            except Exception:
                pass

# ── Formatting ────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER: return True
    return update.effective_user.username == ALLOWED_USER

def pct_bar(val, target, length=10):
    filled = min(int((val / target) * length), length)
    return "▓" * filled + "░" * (length - filled)

def format_day(meals: list, date_str: str, label: str) -> str:
    cap = label.capitalize()
    if not meals:
        if label == "today":
            return "Nothing logged today yet.\n\nSend a meal like:\n`chicken rice 400 35 45 8 2`\n_(name cal protein carbs fat fiber)_"
        return f"Nothing logged {label}."
    totals = {k: 0 for k in TARGETS}
    lines = [f"📋 *{cap} — {date_str}*\n"]
    for i, m in enumerate(meals, 1):
        lines.append(f"{i}. *{m['name']}* `{fmt12(m['meal_time'])}`")
        lines.append(f"   {round(m['cal'])}kcal · {round(m['protein'])}p · {round(m['carbs'])}c · {round(m['fat'])}f · {round(m['fiber'])}fb")
        for k in totals:
            totals[k] += float(m.get(k) or 0)
    lines.append("\n📊 *Progress:*")
    for k, lbl, unit in [("cal","Calories","kcal"),("protein","Protein","g"),("carbs","Carbs","g"),("fat","Fat","g"),("fiber","Fiber","g")]:
        v, t = round(totals[k]), TARGETS[k]
        over = " ⚠️ over" if v > t else ""
        lines.append(f"`{lbl:<8}` {pct_bar(v,t)} {min(round((v/t)*100),100)}%  {v}/{t}{unit}{over}")
    lines.append("\n🎯 *Remaining:*")
    for k, lbl, unit in [("cal","Cal","kcal"),("protein","Pro","g"),("carbs","Carbs","g"),("fat","Fat","g"),("fiber","Fiber","g")]:
        rem = TARGETS[k] - totals[k]
        if rem <= 0:
            lines.append(f"{'✅' if rem == 0 else '⚠️'} {lbl}: {'done' if rem == 0 else f'{abs(round(rem))}{unit} over'}")
        else:
            lines.append(f"· {lbl}: {round(rem)}{unit}")
    return "\n".join(lines)

def numbered_meals_list(meals: list, title: str) -> str:
    lines = [f"{title}\n"]
    for i, m in enumerate(meals, 1):
        lines.append(
            f"*{i}.* {m['name']} `{fmt12(m['meal_time'])}`\n"
            f"   `{round(m['cal'])} kcal · {round(m['protein'])}g pro · "
            f"{round(m['carbs'])}g carbs · {round(m['fat'])}g fat · {round(m['fiber'])}g fiber`"
        )
    return "\n".join(lines)

# ── Handlers ──────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "👋 *NutriTrack Bot*\n\n"
        "Log meals by sending:\n`meal name  cal  protein  carbs  fat  fiber`\n\n"
        "*Quick examples:*\n"
        "`chicken salad 320 38 12 12 2`\n"
        "`oats banana 380 12p 60c 6f 4fb`\n"
        "`chicken salad 320 38 12 12 at 1:30pm`\n\n"
        "Midnight–4am IST counts as the previous day.\n\n"
        "*/help* — full command reference",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "📖 *NutriTrack — Command Reference*\n\n"

        "── *Viewing* ──\n"
        "*/today* — meals + full macro progress for today\n"
        "*/yesterday* — same for yesterday\n"
        "*/week* — last 7 days summary + averages\n"
        "*/streak* — current consecutive logging streak\n"
        "*/targets* — your daily macro targets\n\n"

        "── *Editing today* ──\n"
        "*/edit* — numbered list of today's meals\n"
        "*/edit N* — show meal N's current macros\n"
        "*/edit N cal pro carbs fat fiber* — update meal N\n"
        "  _e.g._ `/edit 2 350 35 10 8 2`\n"
        "*/delete* — numbered list, pick one to remove\n"
        "*/delete N* — remove meal N\n"
        "*/undo* — remove the last logged meal\n\n"

        "── *Presets* ──\n"
        "*/save name cal pro carbs fat fiber* — save a preset\n"
        "  _e.g._ `/save whey 131 30 1 0.6 0`\n"
        "*/presets* — list all saved presets\n"
        "*/log name* — instantly log a preset (no confirmation)\n"
        "  _e.g._ `/log whey`\n"
        "*/unsave name* — delete a preset\n"
        "_Tip: just type a preset name alone to log with confirmation._\n\n"

        "── *Utilities* ──\n"
        "*/copy* — copy all of yesterday's meals to today\n"
        "*/remind 9pm* — nudge at 9pm if nothing logged yet\n"
        "*/remind off* — disable reminder\n"
        "*/ping* — check server + database response time\n\n"

        "── *Logging format* ──\n"
        "`name  cal  protein  carbs  fat  fiber`\n\n"
        "*Plain numbers (positional):*\n"
        "`chicken salad 320 38 12 12 2`\n\n"
        "*Tagged macros (any order, mix ok):*\n"
        "`oats banana 380 12p 60c 6f 4fb`\n\n"
        "*With custom time:*\n"
        "`chicken salad 320 38 12 12 at 1:30pm`\n\n"
        "*With custom time + date:*\n"
        "`meal 320 38 12 12 at 1:30pm yesterday`\n"
        "`meal 320 38 12 12 at 1:30pm 31 May`\n"
        "`meal 320 38 12 12 at 1:30pm 2026-05-31`\n\n"
        "⏰ Midnight–4am IST always counts as the previous day.",
        parse_mode="Markdown"
    )

async def today_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        meals = await db_fetch_today()
        await update.message.reply_text(format_day(meals, today_str(), "today"), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def yesterday_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        yd = yesterday_str()
        meals = await db_fetch_date(yd)
        await update.message.reply_text(format_day(meals, yd, "yesterday"), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def week_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        today = today_str()
        today_dt = datetime.strptime(today, "%Y-%m-%d")
        from_date = (today_dt - timedelta(days=6)).strftime("%Y-%m-%d")
        meals = await db_fetch_range(from_date, today)
        by_date: dict[str, list] = {}
        for m in meals:
            by_date.setdefault(m["meal_date"], []).append(m)
        days = [(today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        yd = yesterday_str()
        lines = ["📅 *Last 7 days*\n"]
        totals_list = []
        for d in days:
            d_meals = by_date.get(d, [])
            dlabel = datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")
            suffix = " ← today" if d == today else (" ← yesterday" if d == yd else "")
            if not d_meals:
                lines.append(f"░ `{dlabel}` — not logged{suffix}"); continue
            tot = {k: round(sum(float(m.get(k) or 0) for m in d_meals)) for k in TARGETS}
            totals_list.append(tot)
            hit = "✅" if tot["cal"] >= TARGETS["cal"] * 0.9 and tot["protein"] >= TARGETS["protein"] * 0.9 else "·"
            lines.append(f"{hit} `{dlabel}` {tot['cal']} kcal · {tot['protein']}g pro{suffix}")
        if totals_list:
            n = len(totals_list)
            lines.append(f"\n📊 *Averages ({n} logged day{'s' if n>1 else ''}):*")
            for k, lbl, unit in [("cal","Cal","kcal"),("protein","Pro","g"),("carbs","Carbs","g"),("fat","Fat","g"),("fiber","Fiber","g")]:
                avg = round(sum(t[k] for t in totals_list) / n)
                pct = min(round((avg / TARGETS[k]) * 100), 100)
                lines.append(f"`{lbl:<6}` {pct_bar(avg, TARGETS[k])} {pct}%  {avg}/{TARGETS[k]}{unit}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def streak_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        today = today_str()
        today_dt = datetime.strptime(today, "%Y-%m-%d")
        from_date = (today_dt - timedelta(days=89)).strftime("%Y-%m-%d")
        meals = await db_fetch_range(from_date, today)
        logged_dates = set(m["meal_date"] for m in meals)
        streak = 0
        d = today_dt
        while d.strftime("%Y-%m-%d") in logged_dates:
            streak += 1; d -= timedelta(days=1)
        if streak == 0:
            msg = "📉 *No streak yet*\n\nNothing logged today. Log a meal to start!"
        elif streak == 1:
            msg = "🔥 *Streak: 1 day*\n\nLogged today — keep it going!"
        elif streak < 7:
            msg = f"🔥 *Streak: {streak} days*\n\n{7 - streak} more to hit a week streak!"
        elif streak < 30:
            weeks = streak // 7
            msg = f"🔥 *Streak: {streak} days* — {weeks} week{'s' if weeks>1 else ''} 🏆"
        else:
            msg = f"🏆 *Streak: {streak} days*\n\nAbsolute consistency."
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def targets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    lines = [
        "🎯 *Daily targets*\n",
        f"`Calories` {TARGETS['cal']} kcal",
        f"`Protein ` {TARGETS['protein']}g",
        f"`Carbs   ` {TARGETS['carbs']}g",
        f"`Fat     ` {TARGETS['fat']}g",
        f"`Fiber   ` {TARGETS['fiber']}g",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def edit_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        meals = await db_fetch_today()
        if not meals:
            await update.message.reply_text("Nothing logged today to edit."); return

        args = ctx.args

        if not args:
            text = numbered_meals_list(meals, "✏️ *Today's meals — /edit N to update:*")
            text += "\n\n_Usage:_ `/edit N cal protein carbs fat fiber`"
            await update.message.reply_text(text, parse_mode="Markdown"); return

        try:
            n = int(args[0])
        except ValueError:
            await update.message.reply_text("Usage: `/edit N cal protein carbs fat fiber`\ne.g. `/edit 2 350 35 10 8 2`", parse_mode="Markdown"); return

        if not (1 <= n <= len(meals)):
            await update.message.reply_text(f"No meal #{n}. Today has {len(meals)} meal(s)."); return

        m = meals[n - 1]

        if len(args) == 1:
            # Show this meal's current values
            lines = [
                f"✏️ *Meal #{n}: {m['name']}*\n",
                f"Time: `{fmt12(m['meal_time'])}`",
                f"Cal: `{round(m['cal'])}` · Pro: `{round(m['protein'])}g`",
                f"Carbs: `{round(m['carbs'])}g` · Fat: `{round(m['fat'])}g` · Fiber: `{round(m['fiber'])}g`\n",
                f"_To update:_ `/edit {n} cal protein carbs fat fiber`",
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown"); return

        # Parse new macro values (positional: cal protein carbs fat fiber)
        keys = ["cal", "protein", "carbs", "fat", "fiber"]
        updates = {}
        for i, val_str in enumerate(args[1:6]):
            try:
                updates[keys[i]] = float(val_str)
            except ValueError:
                await update.message.reply_text(f"Invalid value: `{val_str}`", parse_mode="Markdown"); return

        await db_update(m["id"], updates)
        summary = " · ".join(
            f"{round(v)}{'kcal' if k == 'cal' else 'g '+k}"
            for k, v in updates.items()
        )
        await update.message.reply_text(
            f"✅ Updated meal #{n}: *{m['name']}*\n`{summary}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        meals = await db_fetch_today()
        if not meals:
            await update.message.reply_text("Nothing logged today to delete."); return

        if not ctx.args:
            text = numbered_meals_list(meals, "🗑 *Today's meals — /delete N to remove:*")
            await update.message.reply_text(text, parse_mode="Markdown"); return

        try:
            n = int(ctx.args[0])
        except ValueError:
            await update.message.reply_text("Usage: `/delete N` — e.g. `/delete 2`", parse_mode="Markdown"); return

        if not (1 <= n <= len(meals)):
            await update.message.reply_text(f"No meal #{n}. Today has {len(meals)} logged."); return

        target = meals[n - 1]
        await db_delete(target["id"])
        await update.message.reply_text(f"🗑 Removed #{n}: *{target['name']}*", parse_mode="Markdown")
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

async def save_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/save name cal protein carbs fat fiber`\n"
            "e.g. `/save whey 131 30 1 0.6 0`",
            parse_mode="Markdown"); return
    name = ctx.args[0].lower()
    vals = []
    for v in ctx.args[1:]:
        try: vals.append(float(v))
        except ValueError: pass
    if not vals:
        await update.message.reply_text("❓ No macros found. Format: `/save name cal protein carbs fat fiber`", parse_mode="Markdown"); return
    keys = ["cal", "protein", "carbs", "fat", "fiber"]
    macros = {keys[i]: vals[i] for i in range(min(len(vals), 5))}
    if "cal" not in macros:
        await update.message.reply_text("Need at least calories.", parse_mode="Markdown"); return
    full = {k: macros.get(k, 0) for k in keys}
    try:
        await db_save_preset(name, full)
        await update.message.reply_text(
            f"💾 Saved preset *{name}*\n"
            f"`{round(full['cal'])} kcal · {round(full['protein'])}g pro · "
            f"{round(full['carbs'])}g carbs · {round(full['fat'])}g fat · {round(full['fiber'])}g fiber`\n\n"
            f"Log it any time — just type `{name}` or use `/log {name}`",
            parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}\n\n_Make sure the `presets` table exists in Supabase — see bot.py comments for the SQL._", parse_mode="Markdown")

async def presets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        presets = await db_list_presets()
        if not presets:
            await update.message.reply_text(
                "No presets saved yet.\n\n"
                "Save one: `/save name cal protein carbs fat fiber`",
                parse_mode="Markdown"); return
        lines = ["💾 *Saved presets*\n"]
        for p in presets:
            lines.append(
                f"• *{p['name']}* — {round(p['cal'])} kcal · {round(p['protein'])}g pro · "
                f"{round(p['carbs'])}g carbs · {round(p['fat'])}g fat · {round(p['fiber'])}g fiber"
            )
        lines.append("\nType a name to log with confirmation, or `/log name` to log instantly.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def unsave_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/unsave name`", parse_mode="Markdown"); return
    name = ctx.args[0].lower()
    try:
        preset = await db_fetch_preset(name)
        if not preset:
            await update.message.reply_text(f"No preset named *{name}*.", parse_mode="Markdown"); return
        await db_delete_preset(name)
        await update.message.reply_text(f"🗑 Removed preset *{name}*.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def log_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Instantly log a preset — no confirmation prompt."""
    if not is_allowed(update): return
    if not ctx.args:
        try:
            presets = await db_list_presets()
            if not presets:
                await update.message.reply_text("No presets. Save one: `/save name macros`", parse_mode="Markdown"); return
            lines = ["⚡ *Quick log — /log name:*\n"]
            for p in presets:
                lines.append(f"· `/log {p['name']}` — {round(p['cal'])} kcal · {round(p['protein'])}g pro")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"⚠️ {e}")
        return
    name = ctx.args[0].lower()
    try:
        preset = await db_fetch_preset(name)
        if not preset:
            await update.message.reply_text(f"No preset named *{name}*. Check `/presets`.", parse_mode="Markdown"); return
        meal = {
            "meal_date": today_str(), "meal_time": now_time(),
            "name": preset["name"],
            "cal": preset["cal"], "protein": preset["protein"],
            "carbs": preset["carbs"], "fat": preset["fat"], "fiber": preset["fiber"],
        }
        await db_insert(meal)
        meals = await db_fetch_today()
        totals = {k: sum(float(m.get(k) or 0) for m in meals) for k in TARGETS}
        cal_pct = min(round((totals["cal"] / TARGETS["cal"]) * 100), 100)
        pro_pct = min(round((totals["protein"] / TARGETS["protein"]) * 100), 100)
        await update.message.reply_text(
            f"⚡ Logged: *{meal['name']}* `{fmt12(meal['meal_time'])}`\n"
            f"`{round(meal['cal'])} kcal · {round(meal['protein'])}g protein`\n\n"
            f"Today: {round(totals['cal'])}/{TARGETS['cal']} kcal ({cal_pct}%) · "
            f"{round(totals['protein'])}/{TARGETS['protein']}g pro ({pro_pct}%)",
            parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def copy_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        yd = yesterday_str()
        meals = await db_fetch_date(yd)
        if not meals:
            await update.message.reply_text(f"Nothing logged {yd} to copy."); return
        lines = [f"📋 Copy {len(meals)} meals from yesterday to today?\n"]
        for m in meals:
            lines.append(f"• *{m['name']}* `{fmt12(m['meal_time'])}` — {round(m['cal'])} kcal")
        pending_id = str(uuid.uuid4())[:8]
        PENDING[pending_id] = {"action": "copy", "data": meals}
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Copy all", callback_data=f"copy:{pending_id}"),
            InlineKeyboardButton("❌ Cancel",   callback_data=f"cancel:{pending_id}")
        ]])
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        if REMINDER:
            await update.message.reply_text(
                f"⏰ Reminder set for *{fmt12(REMINDER['time'])}* IST\n"
                "Use `/remind off` to disable.",
                parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "No reminder set.\n\nUsage: `/remind 9pm` or `/remind 21:00`",
                parse_mode="Markdown")
        return
    arg = " ".join(ctx.args).lower().strip()
    if arg == "off":
        REMINDER.clear(); await update.message.reply_text("⏰ Reminder disabled."); return
    t = parse_reminder_time(arg)
    if not t:
        await update.message.reply_text("❓ Couldn't parse time. Try `/remind 9pm` or `/remind 21:00`", parse_mode="Markdown"); return
    REMINDER["time"] = t
    REMINDER["chat_id"] = update.effective_chat.id
    REMINDER_SENT.clear()
    await update.message.reply_text(
        f"⏰ Reminder set for *{fmt12(t)}* IST\n"
        "I'll nudge you if nothing's logged by then.\n"
        "Use `/remind off` to disable.",
        parse_mode="Markdown")

async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = await update.message.reply_text("⏳ Checking...")
    t0 = _time.time()
    try:
        await db_fetch_date(today_str())
        ms = round((_time.time() - t0) * 1000)
        await msg.edit_text(f"✅ Online — DB responded in {ms}ms")
    except Exception as e:
        await msg.edit_text(f"⚠️ DB error: {e}")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text.strip()
    if not text: return

    # Check if text matches a saved preset (only for short digit-free input)
    if len(text) <= 40 and not any(c.isdigit() for c in text):
        try:
            preset = await db_fetch_preset(text.strip().lower())
            if preset:
                meal = {
                    "meal_date": today_str(), "meal_time": now_time(),
                    "name": preset["name"],
                    "cal": preset["cal"], "protein": preset["protein"],
                    "carbs": preset["carbs"], "fat": preset["fat"], "fiber": preset["fiber"],
                }
                pending_id = str(uuid.uuid4())[:8]
                PENDING[pending_id] = {"action": "log", "data": meal}
                summary = (
                    f"⚡ *{meal['name']}* (preset)\n"
                    f"`{round(meal['cal'])} kcal  ·  {round(meal['protein'])}g protein`\n"
                    f"`{round(meal['carbs'])}g carbs  ·  {round(meal['fat'])}g fat  ·  {round(meal['fiber'])}g fiber`\n\n"
                    f"Log at *{fmt12(meal['meal_time'])}* today?"
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚡ Log it", callback_data=f"log:{pending_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{pending_id}")
                ]])
                await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=keyboard)
                return
        except Exception:
            pass  # fall through to normal parsing

    # Normal macro parsing
    meal, err = parse_message(text)
    if err:
        await update.message.reply_text(err, parse_mode="Markdown"); return

    pending_id = str(uuid.uuid4())[:8]
    PENDING[pending_id] = {"action": "log", "data": meal}
    dl = human_date(meal['meal_date'])
    summary = (
        f"*{meal['name']}*\n"
        f"`{round(meal['cal'])} kcal  ·  {round(meal['protein'])}g protein`\n"
        f"`{round(meal['carbs'])}g carbs  ·  {round(meal['fat'])}g fat  ·  {round(meal['fiber'])}g fiber`\n\n"
        f"Log at *{fmt12(meal['meal_time'])}* on {dl}?"
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

    pending = PENDING.pop(pending_id, None)
    if not pending:
        await query.edit_message_text("⚠️ Session expired. Please try again.")
        return

    if action == "log":
        meal = pending["data"]
        try:
            await db_insert(meal)
            meals = await db_fetch_today()
            totals = {k: sum(float(m.get(k) or 0) for m in meals) for k in TARGETS}
            cal_pct = min(round((totals["cal"] / TARGETS["cal"]) * 100), 100)
            pro_pct = min(round((totals["protein"] / TARGETS["protein"]) * 100), 100)
            await query.edit_message_text(
                f"✅ Logged: *{meal['name']}*\n"
                f"`{round(meal['cal'])} kcal · {round(meal['protein'])}g protein`\n\n"
                f"Today: {round(totals['cal'])}/{TARGETS['cal']} kcal ({cal_pct}%) · "
                f"{round(totals['protein'])}/{TARGETS['protein']}g pro ({pro_pct}%)",
                parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"⚠️ Failed to log: {e}")

    elif action == "copy":
        meals_to_copy = pending["data"]
        today = today_str()
        try:
            count = 0
            for m in meals_to_copy:
                new_meal = {
                    "meal_date": today,
                    "meal_time": m["meal_time"],
                    "name": m["name"],
                    "cal": m["cal"], "protein": m["protein"],
                    "carbs": m["carbs"], "fat": m["fat"], "fiber": m.get("fiber", 0),
                }
                await db_insert(new_meal)
                count += 1
            await query.edit_message_text(f"✅ Copied {count} meal{'s' if count!=1 else ''} from yesterday to today.")
        except Exception as e:
            await query.edit_message_text(f"⚠️ Failed to copy: {e}")

# ── Main ──────────────────────────────────────────────────────
async def main():
    global APP
    threading.Thread(target=run_health_server, daemon=True).start()
    print(f"Health server on port {PORT}")

    APP = Application.builder().token(TELEGRAM_TOKEN).build()
    APP.add_handler(CommandHandler("start",     start))
    APP.add_handler(CommandHandler("help",      help_cmd))
    APP.add_handler(CommandHandler("today",     today_cmd))
    APP.add_handler(CommandHandler("yesterday", yesterday_cmd))
    APP.add_handler(CommandHandler("week",      week_cmd))
    APP.add_handler(CommandHandler("streak",    streak_cmd))
    APP.add_handler(CommandHandler("targets",   targets_cmd))
    APP.add_handler(CommandHandler("edit",      edit_cmd))
    APP.add_handler(CommandHandler("delete",    delete_cmd))
    APP.add_handler(CommandHandler("undo",      undo_cmd))
    APP.add_handler(CommandHandler("save",      save_cmd))
    APP.add_handler(CommandHandler("presets",   presets_cmd))
    APP.add_handler(CommandHandler("unsave",    unsave_cmd))
    APP.add_handler(CommandHandler("log",       log_cmd))
    APP.add_handler(CommandHandler("copy",      copy_cmd))
    APP.add_handler(CommandHandler("remind",    remind_cmd))
    APP.add_handler(CommandHandler("ping",      ping_cmd))
    APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    APP.add_handler(CallbackQueryHandler(handle_callback))

    await APP.initialize()
    await APP.start()
    print("NutriTrack bot running...")
    await APP.updater.start_polling(drop_pending_updates=True)

    # Start background tasks
    asyncio.create_task(keep_alive())
    asyncio.create_task(reminder_loop())

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await APP.updater.stop()
        await APP.stop()
        await APP.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
