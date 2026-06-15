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
SELF_URL       = os.environ.get("RENDER_EXTERNAL_URL", "")

SUPABASE_API    = f"{SUPABASE_URL}/rest/v1/meals"
PRESETS_API     = f"{SUPABASE_URL}/rest/v1/presets"
CHEAT_DAYS_API  = f"{SUPABASE_URL}/rest/v1/cheat_days"
SETTINGS_API    = f"{SUPABASE_URL}/rest/v1/settings"
ACTIVITIES_API  = f"{SUPABASE_URL}/rest/v1/activities"

# ── SQL: run once in Supabase SQL editor ──────────────────────
# -- existing tables (meals, presets, cheat_days, settings) unchanged
#
# -- NEW: activities table
# CREATE TABLE activities (
#   id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
#   activity_date date NOT NULL,
#   activity text NOT NULL,
#   duration_minutes integer NOT NULL,
#   intensity text NOT NULL,
#   calories_burned integer NOT NULL,
#   met_value real NOT NULL,
#   logged_at timestamptz DEFAULT now()
# );
# ALTER TABLE activities ENABLE ROW LEVEL SECURITY;
# CREATE POLICY "anon_all" ON activities FOR ALL TO anon USING (true) WITH CHECK (true);
#
# -- NEW: add activity streak keys to settings table
# INSERT INTO settings (key, value) VALUES
#   ('act_freeze_balance', '0'),
#   ('act_streak_milestone_credited', '0')
# ON CONFLICT (key) DO NOTHING;
#
# -- NEW: daily step count (synced via iOS Shortcut)
# CREATE TABLE daily_steps (
#   step_date date PRIMARY KEY,
#   steps integer NOT NULL,
#   synced_at timestamptz DEFAULT now()
# );
# ALTER TABLE daily_steps ENABLE ROW LEVEL SECURITY;
# CREATE POLICY "anon_all" ON daily_steps FOR ALL TO anon USING (true) WITH CHECK (true);
# ─────────────────────────────────────────────────────────────

HEADERS  = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
TARGETS  = {"cal": 1850, "protein": 145, "carbs": 160, "fat": 55, "fiber": 30}

# Nutrition streak
GOOD_DAY_MEALS   = 2
GOOD_DAY_PROTEIN = TARGETS["protein"] * 0.6   # 87g
FREEZE_EVERY     = 6
FREEZE_MAX       = 2

# Activity streak
WEIGHT_KG        = 74    # for MET calorie estimation
ACT_MIN_CALS     = 150   # kcal burned minimum to count as a valid activity day
ACT_FREEZE_EVERY = 6
ACT_FREEZE_MAX   = 2

# MET values (metabolic equivalent) by activity and intensity
# Calories = MET × weight_kg × hours
METS = {
    # Values from the 2011 Compendium of Physical Activities (Ainsworth et al.)
    # Calories = MET × 74kg × (duration_min / 60)
    "walk":      {"light": 2.8, "moderate": 3.5, "vigorous": 5.0},   # 2mph / 3mph / 4mph
    "jog":       {"light": 6.0, "moderate": 7.0, "vigorous": 9.0},
    "run":       {"light": 8.3, "moderate": 9.8, "vigorous": 12.8},  # 5mph / 6mph / 7.5mph
    "badminton": {"light": 4.5, "moderate": 5.5, "vigorous": 7.0},   # recreational / social / competitive
    "basketball":{"light": 4.5, "moderate": 6.5, "vigorous": 8.0},   # shooting baskets / recreational / game
    "swimming":  {"light": 5.0, "moderate": 5.8, "vigorous": 9.8},   # leisurely / moderate / butterfly vigorous
    "cycling":   {"light": 4.0, "moderate": 6.8, "vigorous": 10.0},  # <10mph / 12-14mph / 16-19mph
    "yoga":      {"light": 2.0, "moderate": 2.5, "vigorous": 4.0},   # restorative / hatha / power/vinyasa
    "gym":       {"light": 3.5, "moderate": 5.0, "vigorous": 6.0},   # weight training: light / moderate / vigorous
    "football":  {"light": 5.0, "moderate": 7.0, "vigorous": 10.0},  # casual / recreational / competitive
    "cricket":   {"light": 3.5, "moderate": 4.8, "vigorous": 6.0},   # fielding / batting / bowling hard
    "dancing":   {"light": 3.0, "moderate": 5.0, "vigorous": 7.8},   # slow / general / aerobic/vigorous
    "hiking":    {"light": 4.0, "moderate": 5.3, "vigorous": 7.0},   # flat / cross-country / steep incline
    "sex":       {"light": 1.8, "moderate": 3.0, "vigorous": 5.8},   # passive / active / very vigorous
    "tennis":    {"light": 5.0, "moderate": 7.3, "vigorous": 8.0},   # doubles / singles / competitive
    "squash":    {"light": 7.3, "moderate": 9.0, "vigorous": 12.1},  # recreational / vigorous / competitive
    "volleyball":{"light": 3.0, "moderate": 4.0, "vigorous": 8.0},   # noncompetitive / recreational / beach competitive
    "boxing":    {"light": 6.0, "moderate": 9.0, "vigorous": 12.8},  # punching bag / moderate sparring / ring
    "skipping":  {"light": 8.8, "moderate": 10.0,"vigorous": 12.3},  # slow / moderate / fast rope
    "stair":     {"light": 4.0, "moderate": 6.0, "vigorous": 8.8},   # slow / moderate / fast climbing
    "pilates":   {"light": 2.5, "moderate": 3.0, "vigorous": 4.0},
    "crossfit":  {"light": 5.0, "moderate": 8.0, "vigorous": 14.0},  # HIIT/CrossFit vigorous = 14 METs
}

# Keyword → canonical activity name (sorted by length desc at match time)
ACTIVITY_ALIASES = {
    "jump rope": "skipping", "weightlifting": "gym",
    "basketball": "basketball", "badminton": "badminton", "volleyball": "volleyball",
    "swimming": "swimming", "crossfit": "crossfit", "trekking": "hiking",
    "sprinting": "run", "jogging": "jog", "walking": "walk", "cycling": "cycling",
    "dancing": "dancing", "pilates": "pilates", "squash": "squash", "tennis": "tennis",
    "cricket": "cricket", "football": "football", "soccer": "football", "boxing": "boxing",
    "skipping": "skipping", "biking": "cycling", "lifting": "gym", "stairs": "stair",
    "hiking": "hiking", "zumba": "dancing", "hoops": "basketball", "shuttle": "badminton",
    "sprint": "run", "swim": "swimming", "cycle": "cycling", "dance": "dancing",
    "hike": "hiking", "trek": "hiking", "bike": "cycling", "rope": "skipping",
    "stair": "stair", "hiit": "crossfit", "yoga": "yoga", "gym": "gym",
    "run": "run", "jog": "jog", "sex": "sex", "box": "boxing",
    "walk": "walk",
}

ACTIVITY_ICONS = {
    "walk": "🚶", "jog": "🏃", "run": "🏃",
    "badminton": "🏸", "basketball": "🏀",
    "swimming": "🏊", "cycling": "🚴",
    "yoga": "🧘", "gym": "🏋️",
    "football": "⚽", "cricket": "🏏",
    "dancing": "💃", "hiking": "🥾",
    "sex": "🔥", "tennis": "🎾",
    "squash": "🎾", "volleyball": "🏐",
    "boxing": "🥊", "skipping": "🏃",
    "stair": "🪜", "pilates": "🤸", "crossfit": "💪",
}

# ── State ─────────────────────────────────────────────────────
PENDING       = {}   # {id: {"action": str, "data": any}}
REMINDER      = {}   # {"time": "HH:MM", "chat_id": int}
REMINDER_SENT = {}   # {"date": "YYYY-MM-DD"}
APP           = None

# ── Health check server ───────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"NutriTrack bot is running.")
    def log_message(self, *args): pass

def run_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()

# ── Time helpers ──────────────────────────────────────────────
def ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))

def today_str():
    now = ist_now()
    if now.hour < 4: now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")

def yesterday_str():
    now = ist_now()
    if now.hour < 4: now -= timedelta(days=1)
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")

def now_time():
    return ist_now().strftime("%H:%M")

def fmt12(t: str) -> str:
    try:
        hh, mm = map(int, t.split(":"))
        return f"{hh%12 or 12}:{mm:02d} {'AM' if hh<12 else 'PM'}"
    except Exception: return t

def human_date(date_str: str) -> str:
    if date_str == today_str():     return "today"
    if date_str == yesterday_str(): return "yesterday"
    try: return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b")
    except Exception: return date_str

def parse_reminder_time(text: str):
    m = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', text.strip(), re.IGNORECASE)
    if not m: return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hh != 12: hh += 12
    elif ampm == "am" and hh == 12: hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59): return None
    return f"{hh:02d}:{mm:02d}"

# ── Good-day definition ───────────────────────────────────────
def is_good_day(meals: list) -> bool:
    """≥2 meals AND ≥60% protein target."""
    if len(meals) < GOOD_DAY_MEALS: return False
    return sum(float(m.get("protein") or 0) for m in meals) >= GOOD_DAY_PROTEIN

def is_valid_activity_day(activities: list) -> bool:
    """At least one activity that burned ≥ACT_MIN_CALS."""
    return any(int(a.get("calories_burned", 0)) >= ACT_MIN_CALS for a in activities)

# ── Parsing ───────────────────────────────────────────────────
def parse_time_from_text(text: str):
    time_pat = r'(?:at|@)\s*(\d{1,2}):(\d{2})\s*(am|pm)?'
    m = re.search(time_pat, text, re.IGNORECASE)
    if not m: return text, None, None
    hh, mm = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hh != 12: hh += 12
    elif ampm == "am" and hh == 12: hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59): return text, None, None
    time_str = f"{hh:02d}:{mm:02d}"
    after = text[m.end():].strip()
    now = ist_now()
    date_str, date_consumed = None, 0
    MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
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

def parse_workout(text: str):
    """Parse activity from: '<activity> <duration> [intensity]'
    e.g. 'badminton 45min moderate' or 'swimming 1.5h vigorous'
    """
    t = text.strip().lower()

    # Duration — look for Nh/Nhr/Nmin/Nm or bare N (assume minutes)
    dur_m = re.search(r'(\d+\.?\d*)\s*(h(?:ours?|rs?)?|min(?:utes?)?|m\b)', t)
    if dur_m:
        val = float(dur_m.group(1))
        unit = dur_m.group(2)
        duration_min = int(val * 60) if unit.startswith("h") else int(val)
    else:
        bare = re.search(r'(\d+)', t)
        if bare:
            duration_min = int(bare.group(1))
        else:
            return None, "❓ Include duration, e.g. `/workout badminton 45min moderate`\nor `/workout walk 30 light`"

    if not (1 <= duration_min <= 600):
        return None, "❓ Duration seems off. Use something like `45min` or `1.5h`."

    # Intensity
    intensity = "moderate"
    if re.search(r'\b(vigorous|intense|hard|high)\b', t):
        intensity = "vigorous"
    elif re.search(r'\b(light|easy|slow|casual|gentle|low)\b', t):
        intensity = "light"

    # Activity — longest alias first to avoid "run" matching inside "running"
    activity = None
    for alias in sorted(ACTIVITY_ALIASES.keys(), key=len, reverse=True):
        if alias in t:
            activity = ACTIVITY_ALIASES[alias]
            break

    if not activity:
        supported = "walk · run · jog · badminton · basketball · swimming · cycling · yoga · gym · football · cricket · dancing · hiking · tennis · squash · boxing · skipping · crossfit · pilates · sex"
        return None, f"❓ Didn't recognise that activity.\n\nSupported: _{supported}_"

    met = METS[activity][intensity]
    calories = round(met * WEIGHT_KG * duration_min / 60)

    return {
        "activity_date": today_str(),
        "activity": activity,
        "duration_minutes": duration_min,
        "intensity": intensity,
        "calories_burned": calories,
        "met_value": met,
    }, None

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
        r = await c.delete(f"{SUPABASE_API}?id=eq.{meal_id}", headers={**HEADERS,"Prefer":"return=minimal"}, timeout=10)
        r.raise_for_status()

async def db_update(meal_id: str, updates: dict):
    async with httpx.AsyncClient() as c:
        r = await c.patch(f"{SUPABASE_API}?id=eq.{meal_id}", headers={**HEADERS,"Prefer":"return=representation"}, json=updates, timeout=10)
        r.raise_for_status(); return r.json()

# ── DB — presets ──────────────────────────────────────────────
async def db_save_preset(name: str, macros: dict):
    async with httpx.AsyncClient() as c:
        r = await c.post(PRESETS_API, headers={**HEADERS,"Prefer":"resolution=merge-duplicates,return=representation"}, json={"name":name.lower(),**macros}, timeout=10)
        r.raise_for_status(); return r.json()

async def db_list_presets():
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{PRESETS_API}?order=name.asc", headers=HEADERS, timeout=10)
        r.raise_for_status(); return r.json()

async def db_fetch_preset(name: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{PRESETS_API}?name=eq.{name.lower()}", headers=HEADERS, timeout=10)
        r.raise_for_status(); data = r.json(); return data[0] if data else None

async def db_delete_preset(name: str):
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{PRESETS_API}?name=eq.{name.lower()}", headers={**HEADERS,"Prefer":"return=minimal"}, timeout=10)
        r.raise_for_status()

# ── DB — cheat days ───────────────────────────────────────────
async def db_list_cheat_days() -> set:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{CHEAT_DAYS_API}?order=date.desc", headers=HEADERS, timeout=10)
        r.raise_for_status(); return {row["date"] for row in r.json()}

async def db_add_cheat_day(date_str: str):
    async with httpx.AsyncClient() as c:
        r = await c.post(CHEAT_DAYS_API, headers={**HEADERS,"Prefer":"return=minimal"}, json={"date": date_str}, timeout=10)
        r.raise_for_status()

# ── DB — settings ─────────────────────────────────────────────
async def db_get_setting(key: str) -> str:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{SETTINGS_API}?key=eq.{key}", headers=HEADERS, timeout=10)
        r.raise_for_status(); data = r.json()
        return data[0]["value"] if data else "0"

async def db_set_setting(key: str, value: str):
    async with httpx.AsyncClient() as c:
        r = await c.patch(f"{SETTINGS_API}?key=eq.{key}", headers={**HEADERS,"Prefer":"return=minimal"}, json={"value": value}, timeout=10)
        r.raise_for_status()

# ── DB — activities ───────────────────────────────────────────
async def db_insert_activity(act: dict):
    async with httpx.AsyncClient() as c:
        r = await c.post(ACTIVITIES_API, headers={**HEADERS,"Prefer":"return=representation"}, json=act, timeout=10)
        r.raise_for_status(); return r.json()

async def db_fetch_activities(date_str: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{ACTIVITIES_API}?activity_date=eq.{date_str}&order=logged_at.asc", headers=HEADERS, timeout=10)
        r.raise_for_status(); return r.json()

async def db_fetch_activities_range(from_date: str, to_date: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{ACTIVITIES_API}?activity_date=gte.{from_date}&activity_date=lte.{to_date}&order=activity_date.asc,logged_at.asc",
            headers=HEADERS, timeout=10)
        r.raise_for_status(); return r.json()

async def db_delete_activity(act_id: str):
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{ACTIVITIES_API}?id=eq.{act_id}", headers={**HEADERS,"Prefer":"return=minimal"}, timeout=10)
        r.raise_for_status()

# ── Nutrition streak ──────────────────────────────────────────
async def compute_streak(meals_by_date: dict | None = None, cheat_dates: set | None = None) -> int:
    today = today_str()
    today_dt = datetime.strptime(today, "%Y-%m-%d")

    if meals_by_date is None:
        from_date = (today_dt - timedelta(days=89)).strftime("%Y-%m-%d")
        raw = await db_fetch_range(from_date, today)
        meals_by_date = {}
        for m in raw:
            meals_by_date.setdefault(m["meal_date"], []).append(m)

    if cheat_dates is None:
        cheat_dates = await db_list_cheat_days()

    today_good = is_good_day(meals_by_date.get(today, [])) or today in cheat_dates
    start_offset = 0 if today_good else 1

    streak = 0
    for i in range(start_offset, 90):
        d = (today_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        if is_good_day(meals_by_date.get(d, [])) or d in cheat_dates:
            streak += 1
        else:
            break
    return streak

async def check_and_credit_freezes(chat_id: int):
    """Credit a nutrition freeze at every 6-day milestone."""
    try:
        streak = await compute_streak()
        milestone_reached = streak // FREEZE_EVERY
        if milestone_reached == 0: return

        last_credited = int(await db_get_setting("streak_milestone_credited"))
        if milestone_reached <= last_credited: return

        balance = int(await db_get_setting("freeze_balance"))
        new_freezes = milestone_reached - last_credited
        new_balance = min(balance + new_freezes, FREEZE_MAX)
        added = new_balance - balance

        await db_set_setting("freeze_balance", str(new_balance))
        await db_set_setting("streak_milestone_credited", str(milestone_reached))

        if added > 0 and APP:
            await APP.bot.send_message(
                chat_id,
                f"🎉 *{streak}-day nutrition streak!*\n\n"
                f"You earned a freeze ❄️\n"
                f"Balance: {new_balance}/{FREEZE_MAX}\n\n"
                f"Use `/cheatday` any time to protect your streak.",
                parse_mode="Markdown"
            )
    except Exception:
        pass

# ── Activity streak ───────────────────────────────────────────
async def compute_activity_streak(acts_by_date: dict | None = None) -> int:
    today = today_str()
    today_dt = datetime.strptime(today, "%Y-%m-%d")

    if acts_by_date is None:
        from_date = (today_dt - timedelta(days=89)).strftime("%Y-%m-%d")
        raw = await db_fetch_activities_range(from_date, today)
        acts_by_date = {}
        for a in raw:
            acts_by_date.setdefault(a["activity_date"], []).append(a)

    today_valid = is_valid_activity_day(acts_by_date.get(today, []))
    start_offset = 0 if today_valid else 1

    streak = 0
    for i in range(start_offset, 90):
        d = (today_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        if is_valid_activity_day(acts_by_date.get(d, [])):
            streak += 1
        else:
            break
    return streak

async def check_and_credit_act_freezes(chat_id: int):
    """Credit an activity freeze at every 6-day activity milestone."""
    try:
        streak = await compute_activity_streak()
        milestone_reached = streak // ACT_FREEZE_EVERY
        if milestone_reached == 0: return

        last_credited = int(await db_get_setting("act_streak_milestone_credited"))
        if milestone_reached <= last_credited: return

        balance = int(await db_get_setting("act_freeze_balance"))
        new_freezes = milestone_reached - last_credited
        new_balance = min(balance + new_freezes, ACT_FREEZE_MAX)
        added = new_balance - balance

        await db_set_setting("act_freeze_balance", str(new_balance))
        await db_set_setting("act_streak_milestone_credited", str(milestone_reached))

        if added > 0 and APP:
            await APP.bot.send_message(
                chat_id,
                f"🏃 *{streak}-day activity streak!*\n\n"
                f"You earned an activity freeze ❄️\n"
                f"Balance: {new_balance}/{ACT_FREEZE_MAX}",
                parse_mode="Markdown"
            )
    except Exception:
        pass

# ── Background tasks ──────────────────────────────────────────
async def keep_alive():
    if not SELF_URL:
        print("SELF_URL not set — keep-alive disabled"); return
    await asyncio.sleep(30)
    while True:
        try:
            async with httpx.AsyncClient() as c:
                await c.get(SELF_URL, timeout=15)
        except Exception: pass
        await asyncio.sleep(10 * 60)

async def reminder_loop():
    while True:
        await asyncio.sleep(60)
        if not REMINDER or not APP: continue
        now = ist_now()
        today = today_str()
        if f"{now.hour:02d}:{now.minute:02d}" == REMINDER["time"] and REMINDER_SENT.get("date") != today:
            REMINDER_SENT["date"] = today
            try:
                meals = await db_fetch_today()
                if not meals:
                    await APP.bot.send_message(REMINDER["chat_id"],
                        "⏰ *Daily reminder* — nothing logged today yet!\n\nHow's your nutrition going?",
                        parse_mode="Markdown")
            except Exception: pass

# ── Formatting ────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER: return True
    return update.effective_user.username == ALLOWED_USER

def pct_bar(val, target, length=10):
    return "▓" * min(int((val/target)*length), length) + "░" * max(length - int((val/target)*length), 0)

def fmt_activity_line(a: dict) -> str:
    icon = ACTIVITY_ICONS.get(a["activity"], "🏃")
    valid = int(a.get("calories_burned", 0)) >= ACT_MIN_CALS
    badge = "✓" if valid else "·"
    return (f"{badge} {icon} *{a['activity'].title()}* · "
            f"{a['duration_minutes']}min · {a['intensity']} · "
            f"{a['calories_burned']} kcal")

def format_activities_section(activities: list) -> str:
    if not activities:
        return "\n🏃 *Activity:* Nothing logged today"
    total_burned = sum(int(a.get("calories_burned", 0)) for a in activities)
    valid_count = sum(1 for a in activities if int(a.get("calories_burned", 0)) >= ACT_MIN_CALS)
    lines = ["\n🏃 *Activity:*"]
    for a in activities:
        lines.append("   " + fmt_activity_line(a))
    lines.append(f"   Total: {total_burned} kcal burned"
                 + (f" · {valid_count} valid" if len(activities) > 1 else ""))
    return "\n".join(lines)

def format_day(meals: list, date_str: str, label: str,
               cheat: bool = False, activities: list | None = None) -> str:
    cap = label.capitalize()
    cheat_banner = "❄️ *Cheat day — streak protected · excluded from averages*\n\n" if cheat else ""
    act_section = format_activities_section(activities) if activities is not None else ""

    if not meals:
        if label == "today":
            return (cheat_banner +
                    "Nothing logged today yet.\n\n"
                    "Send a meal like:\n`chicken rice 400 35 45 8 2`\n"
                    "_(name cal protein carbs fat fiber)_" + act_section)
        return cheat_banner + f"Nothing logged {label}." + act_section

    totals = {k: 0 for k in TARGETS}
    lines = [f"{cheat_banner}📋 *{cap} — {date_str}*\n"]
    for i, m in enumerate(meals, 1):
        lines.append(f"{i}. *{m['name']}* `{fmt12(m['meal_time'])}`")
        lines.append(f"   {round(m['cal'])} kcal · {round(m['protein'])}p · "
                     f"{round(m['carbs'])}c · {round(m['fat'])}f · {round(m.get('fiber',0))}fb")
        for k in totals: totals[k] += float(m.get(k) or 0)

    if not cheat:
        lines.append("\n📊 *Nutrition progress:*")
        for k, lbl, unit in [("cal","Calories","kcal"),("protein","Protein","g"),
                              ("carbs","Carbs","g"),("fat","Fat","g"),("fiber","Fiber","g")]:
            v, t = round(totals[k]), TARGETS[k]
            over = " ⚠️ over" if v > t else ""
            lines.append(f"`{lbl:<8}` {pct_bar(v,t)} {min(round((v/t)*100),100)}%  {v}/{t}{unit}{over}")
        lines.append("\n🎯 *Remaining:*")
        for k, lbl, unit in [("cal","Cal","kcal"),("protein","Pro","g"),
                              ("carbs","Carbs","g"),("fat","Fat","g"),("fiber","Fiber","g")]:
            rem = TARGETS[k] - totals[k]
            if rem <= 0: lines.append(f"{'✅' if rem==0 else '⚠️'} {lbl}: {'done' if rem==0 else f'{abs(round(rem))}{unit} over'}")
            else: lines.append(f"· {lbl}: {round(rem)}{unit}")

    if act_section:
        lines.append(act_section)

    return "\n".join(lines)

def numbered_meals_list(meals: list, title: str) -> str:
    lines = [f"{title}\n"]
    for i, m in enumerate(meals, 1):
        lines.append(
            f"*{i}.* {m['name']} `{fmt12(m['meal_time'])}`\n"
            f"   `{round(m['cal'])} kcal · {round(m['protein'])}g pro · "
            f"{round(m['carbs'])}g carbs · {round(m['fat'])}g fat · {round(m.get('fiber',0))}g fiber`"
        )
    return "\n".join(lines)

def numbered_activities_list(acts: list, title: str) -> str:
    lines = [f"{title}\n"]
    for i, a in enumerate(acts, 1):
        icon = ACTIVITY_ICONS.get(a["activity"], "🏃")
        lines.append(
            f"*{i}.* {icon} {a['activity'].title()} · {a['duration_minutes']}min · "
            f"{a['intensity']} · {a['calories_burned']} kcal"
        )
    return "\n".join(lines)

# ── Handlers ──────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "👋 *NutriTrack Bot*\n\n"
        "Log meals: `name  cal  protein  carbs  fat  fiber`\n"
        "`chicken salad 320 38 12 12 2`\n\n"
        "Log activity: `/workout badminton 45min moderate`\n\n"
        "Midnight–4am IST counts as the previous day.\n\n"
        "*/help* — full command reference",
        parse_mode="Markdown")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    good_pro = round(GOOD_DAY_PROTEIN)
    await update.message.reply_text(
        "📖 *NutriTrack — Command Reference*\n\n"

        "── *Viewing* ──\n"
        "*/today* — meals + macros + activity for today\n"
        "*/yesterday* — same for yesterday\n"
        "*/week* — last 7 days summary + averages\n"
        "*/streak* — both streaks + freeze balances\n"
        "*/targets* — your daily targets\n\n"

        "── *Editing meals* ──\n"
        "*/edit* — numbered list of today's meals\n"
        "*/edit N* — show meal N's current macros\n"
        "*/edit N cal pro carbs fat fiber* — update meal N\n"
        "*/delete* — numbered list, pick one to remove\n"
        "*/delete N* — remove meal N from today\n"
        "*/undo* — remove the last logged meal\n\n"

        "── *Meal presets* ──\n"
        "*/save name cal pro carbs fat fiber* — save a preset\n"
        "*/presets* — list all saved presets\n"
        "*/log name* — instantly log a preset\n"
        "*/unsave name* — delete a preset\n"
        "_Type a preset name alone to log with confirmation._\n\n"

        "── *Activity* ──\n"
        "*/workout <activity> <duration> [intensity]* — log a workout\n"
        "  e.g. `/workout badminton 45min moderate`\n"
        "       `/workout swimming 1.5h vigorous`\n"
        "       `/workout walk 30 light`\n"
        "*/activities* — today's activity log\n"
        "*/actstreak* — activity streak + freeze balance\n"
        "*/actfreeze* — activity freeze balance + progress\n"
        "*/delworkout N* — remove workout N from today\n\n"
        f"_Valid activity day:_ ≥1 activity burning ≥{ACT_MIN_CALS} kcal\n"
        "_Intensity levels:_ light · moderate · vigorous\n\n"

        "── *Nutrition streak freezes* ──\n"
        "*/freeze* — nutrition freeze balance\n"
        "*/cheatday* — use a freeze for today\n\n"
        f"_Good nutrition day:_ ≥{GOOD_DAY_MEALS} meals + ≥{good_pro}g protein\n"
        f"_Earns a freeze:_ every {FREEZE_EVERY} consecutive good days\n"
        f"_Max held:_ {FREEZE_MAX}\n\n"

        "── *Utilities* ──\n"
        "*/copy* — copy yesterday's meals to today\n"
        "*/remind 9pm* — daily nudge if nothing logged\n"
        "*/remind off* — disable reminder\n"
        "*/ping* — check server + DB response\n\n"

        "── *Meal format* ──\n"
        "`name  cal  protein  carbs  fat  fiber`\n"
        "*Plain:* `chicken salad 320 38 12 12 2`\n"
        "*Tagged:* `oats 380 12p 60c 6f 4fb`\n"
        "*Time:* `meal 320 38 12 12 at 1:30pm`\n"
        "*Date:* `meal 320 38 12 12 at 1:30pm yesterday`\n\n"
        "⏰ Midnight–4am IST counts as the previous day.",
        parse_mode="Markdown")

async def today_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        today = today_str()
        meals, cheat_dates, activities = await asyncio.gather(
            db_fetch_today(), db_list_cheat_days(), db_fetch_activities(today))
        await update.message.reply_text(
            format_day(meals, today, "today", cheat=today in cheat_dates, activities=activities),
            parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def yesterday_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        yd = yesterday_str()
        meals, cheat_dates, activities = await asyncio.gather(
            db_fetch_date(yd), db_list_cheat_days(), db_fetch_activities(yd))
        await update.message.reply_text(
            format_day(meals, yd, "yesterday", cheat=yd in cheat_dates, activities=activities),
            parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def week_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        today = today_str()
        today_dt = datetime.strptime(today, "%Y-%m-%d")
        from_date = (today_dt - timedelta(days=6)).strftime("%Y-%m-%d")
        meals, cheat_dates, acts_raw = await asyncio.gather(
            db_fetch_range(from_date, today),
            db_list_cheat_days(),
            db_fetch_activities_range(from_date, today))

        by_date: dict = {}
        for m in meals:
            by_date.setdefault(m["meal_date"], []).append(m)
        acts_by_date: dict = {}
        for a in acts_raw:
            acts_by_date.setdefault(a["activity_date"], []).append(a)

        days = [(today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        yd = yesterday_str()
        lines = ["📅 *Last 7 days*\n"]
        totals_list = []
        for d in days:
            d_meals = by_date.get(d, [])
            d_acts  = acts_by_date.get(d, [])
            dlabel  = datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")
            suffix  = " ← today" if d == today else (" ← yesterday" if d == yd else "")
            cheat_tag = " ❄️" if d in cheat_dates else ""
            act_burned = sum(int(a.get("calories_burned", 0)) for a in d_acts)
            act_icon = "🏃" if is_valid_activity_day(d_acts) else ("·" if d_acts else " ")

            if not d_meals and d not in cheat_dates:
                act_str = f" {act_icon} {act_burned} kcal" if act_burned else ""
                lines.append(f"░ `{dlabel}` — not logged{act_str}{suffix}"); continue

            tot = {k: round(sum(float(m.get(k) or 0) for m in d_meals)) for k in TARGETS}
            if d not in cheat_dates:
                totals_list.append(tot)
            hit = "✅" if (tot["cal"] >= TARGETS["cal"]*0.9 and tot["protein"] >= TARGETS["protein"]*0.9) else "·"
            if d in cheat_dates:
                lines.append(f"❄️ `{dlabel}` cheat day{suffix}")
            else:
                act_part = f" | {act_icon} {act_burned} kcal" if act_burned else ""
                lines.append(f"{hit} `{dlabel}` {tot['cal']} kcal · {tot['protein']}g pro{act_part}{suffix}{cheat_tag}")

        if totals_list:
            n = len(totals_list)
            lines.append(f"\n📊 *Averages ({n} logged day{'s' if n>1 else ''}, cheat days excluded):*")
            for k, lbl, unit in [("cal","Cal","kcal"),("protein","Pro","g"),("carbs","Carbs","g"),("fat","Fat","g"),("fiber","Fiber","g")]:
                avg = round(sum(t[k] for t in totals_list) / n)
                pct = min(round((avg/TARGETS[k])*100), 100)
                lines.append(f"`{lbl:<6}` {pct_bar(avg, TARGETS[k])} {pct}%  {avg}/{TARGETS[k]}{unit}")

        # Activity week summary
        act_days = sum(1 for d in days if is_valid_activity_day(acts_by_date.get(d, [])))
        total_burned = sum(int(a.get("calories_burned", 0)) for d in days for a in acts_by_date.get(d, []))
        lines.append(f"\n🏃 *Activity:* {act_days}/7 active days · {total_burned} kcal burned total")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def streak_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        await asyncio.gather(
            check_and_credit_freezes(update.effective_chat.id),
            check_and_credit_act_freezes(update.effective_chat.id))

        nut_streak, act_streak, nut_bal_str, act_bal_str = await asyncio.gather(
            compute_streak(), compute_activity_streak(),
            db_get_setting("freeze_balance"),
            db_get_setting("act_freeze_balance"))
        nut_bal = int(nut_bal_str)
        act_bal = int(act_bal_str)
        good_pro = round(GOOD_DAY_PROTEIN)

        lines = ["📊 *Streak Summary*\n"]

        # Nutrition streak
        nut_into = nut_streak % FREEZE_EVERY
        if nut_streak == 0:
            lines.append(f"🥗 *Nutrition streak:* not started\n"
                         f"   _Good day = ≥{GOOD_DAY_MEALS} meals + ≥{good_pro}g protein_")
        else:
            lines.append(f"🥗 *Nutrition streak: {nut_streak} day{'s' if nut_streak>1 else ''}*")
            if nut_bal < FREEZE_MAX:
                if nut_into == 0:
                    lines.append(f"   {pct_bar(0, FREEZE_EVERY)} new period · ❄️ {nut_bal}/{FREEZE_MAX} freezes")
                else:
                    lines.append(f"   {pct_bar(nut_into, FREEZE_EVERY)} {nut_into}/{FREEZE_EVERY} · ❄️ {nut_bal}/{FREEZE_MAX} freezes")
            else:
                lines.append(f"   ❄️ Freezes: {nut_bal}/{FREEZE_MAX} (full)")

        lines.append("")

        # Activity streak
        act_into = act_streak % ACT_FREEZE_EVERY
        if act_streak == 0:
            lines.append(f"🏃 *Activity streak:* not started\n"
                         f"   _Valid day = ≥1 activity burning ≥{ACT_MIN_CALS} kcal_")
        else:
            lines.append(f"🏃 *Activity streak: {act_streak} day{'s' if act_streak>1 else ''}*")
            if act_bal < ACT_FREEZE_MAX:
                if act_into == 0:
                    lines.append(f"   {pct_bar(0, ACT_FREEZE_EVERY)} new period · ❄️ {act_bal}/{ACT_FREEZE_MAX} freezes")
                else:
                    lines.append(f"   {pct_bar(act_into, ACT_FREEZE_EVERY)} {act_into}/{ACT_FREEZE_EVERY} · ❄️ {act_bal}/{ACT_FREEZE_MAX} freezes")
            else:
                lines.append(f"   ❄️ Freezes: {act_bal}/{ACT_FREEZE_MAX} (full)")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def targets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    lines = ["🎯 *Daily targets*\n",
             f"`Calories` {TARGETS['cal']} kcal", f"`Protein ` {TARGETS['protein']}g",
             f"`Carbs   ` {TARGETS['carbs']}g",   f"`Fat     ` {TARGETS['fat']}g",
             f"`Fiber   ` {TARGETS['fiber']}g",
             f"\n🏃 *Activity minimum:* {ACT_MIN_CALS} kcal burned / day"]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def freeze_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        await check_and_credit_freezes(update.effective_chat.id)
        streak, balance_str, cheat_dates = await asyncio.gather(
            compute_streak(), db_get_setting("freeze_balance"), db_list_cheat_days())
        balance = int(balance_str)
        days_into = streak % FREEZE_EVERY
        days_to_next = FREEZE_EVERY - days_into if days_into > 0 else FREEZE_EVERY
        good_pro = round(GOOD_DAY_PROTEIN)

        lines = [
            f"❄️ *Nutrition freezes: {balance}/{FREEZE_MAX}*\n",
            f"*Streak:* {streak} good day{'s' if streak!=1 else ''}\n"
            f"*Progress to next:* {pct_bar(days_into, FREEZE_EVERY)} {days_into}/{FREEZE_EVERY} days",
            f"{days_to_next} more good day{'s' if days_to_next!=1 else ''} to earn one\n",
            f"*Good day:* ≥{GOOD_DAY_MEALS} meals + ≥{good_pro}g protein\n",
        ]
        recent = sorted(cheat_dates, reverse=True)[:5]
        if recent:
            lines.append("*Recent cheat days:*")
            for d in recent:
                lines.append(f"  ❄️ {human_date(d)} ({d})")
            lines.append("")
        lines.append("Use `/cheatday` to activate a freeze for today.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def cheatday_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        today = today_str()
        balance_str, cheat_dates, meals_today = await asyncio.gather(
            db_get_setting("freeze_balance"), db_list_cheat_days(), db_fetch_today())
        balance = int(balance_str)

        if today in cheat_dates:
            await update.message.reply_text(
                "❄️ Today is already a cheat day — streak is safe.", parse_mode="Markdown"); return

        if balance <= 0:
            streak = await compute_streak()
            days_into = streak % FREEZE_EVERY
            days_to = FREEZE_EVERY - days_into if days_into > 0 else FREEZE_EVERY
            await update.message.reply_text(
                f"❌ *No nutrition freezes available.*\n\n"
                f"Streak: {streak} day{'s' if streak!=1 else ''}\n"
                f"Progress: {pct_bar(days_into, FREEZE_EVERY)} {days_into}/{FREEZE_EVERY}\n"
                f"{days_to} more good day{'s' if days_to!=1 else ''} to earn one.",
                parse_mode="Markdown"); return

        already_good = is_good_day(meals_today)
        warning = ("⚠️ _Today already looks like a good day — streak intact without a freeze._\n\n"
                   if already_good else "")

        pending_id = str(uuid.uuid4())[:8]
        PENDING[pending_id] = {"action": "cheatday", "data": {"date": today}}
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("❄️ Activate", callback_data=f"cheatday:{pending_id}"),
            InlineKeyboardButton("Cancel",       callback_data=f"cancel:{pending_id}")
        ]])
        await update.message.reply_text(
            f"{warning}*Activate cheat day for today?*\n\n"
            f"• Streak continues unbroken\n"
            f"• Excluded from 7-day averages\n"
            f"• Balance after: {balance-1}/{FREEZE_MAX}",
            parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

# ── Activity commands ─────────────────────────────────────────
async def workout_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text(
            "🏃 *Log an activity:*\n\n"
            "`/workout <activity> <duration> [intensity]`\n\n"
            "*Examples:*\n"
            "`/workout badminton 45min moderate`\n"
            "`/workout swimming 1.5h vigorous`\n"
            "`/workout walk 30 light`\n"
            "`/workout gym 1h`\n\n"
            "*Activities:* walk · run · jog · badminton · basketball · swimming · cycling · yoga · gym · football · cricket · dancing · hiking · tennis · squash · boxing · skipping · crossfit · pilates\n\n"
            "*Intensity:* light · moderate _(default)_ · vigorous",
            parse_mode="Markdown")
        return

    text = " ".join(ctx.args)
    act, err = parse_workout(text)
    if err:
        await update.message.reply_text(err, parse_mode="Markdown"); return

    icon = ACTIVITY_ICONS.get(act["activity"], "🏃")
    valid = act["calories_burned"] >= ACT_MIN_CALS
    streak_note = f"✓ Counts toward activity streak" if valid else f"· {ACT_MIN_CALS - act['calories_burned']} kcal more needed to count"

    pending_id = str(uuid.uuid4())[:8]
    PENDING[pending_id] = {"action": "workout", "data": act}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Log it", callback_data=f"workout:{pending_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{pending_id}")]])
    await update.message.reply_text(
        f"{icon} *{act['activity'].title()}*\n"
        f"`{act['duration_minutes']} min · {act['intensity']} intensity`\n"
        f"*~{act['calories_burned']} kcal burned* _(MET {act['met_value']} × {WEIGHT_KG}kg)_\n\n"
        f"{streak_note}\n\n"
        f"Log for today ({today_str()})?",
        parse_mode="Markdown", reply_markup=keyboard)

async def activities_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        today = today_str()
        acts = await db_fetch_activities(today)
        if not acts:
            await update.message.reply_text(
                f"🏃 No activities logged today.\n\n"
                f"Use `/workout <activity> <duration>` to log one.",
                parse_mode="Markdown"); return

        total_burned = sum(int(a.get("calories_burned", 0)) for a in acts)
        valid_count  = sum(1 for a in acts if int(a.get("calories_burned", 0)) >= ACT_MIN_CALS)
        lines = [f"🏃 *Today's activities — {today}*\n"]
        for i, a in enumerate(acts, 1):
            icon = ACTIVITY_ICONS.get(a["activity"], "🏃")
            valid = int(a.get("calories_burned", 0)) >= ACT_MIN_CALS
            badge = "✓" if valid else "·"
            lines.append(f"{i}. {badge} {icon} *{a['activity'].title()}* "
                         f"`{a['duration_minutes']}min · {a['intensity']} · {a['calories_burned']} kcal`")
        lines.append(f"\n*Total burned:* {total_burned} kcal")
        if valid_count > 0:
            lines.append(f"*Valid for streak:* {valid_count} activit{'y' if valid_count==1 else 'ies'} ✓")
        lines.append("\n_/delworkout N to remove one_")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def actstreak_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        await check_and_credit_act_freezes(update.effective_chat.id)
        streak, balance_str = await asyncio.gather(
            compute_activity_streak(), db_get_setting("act_freeze_balance"))
        balance = int(balance_str)
        days_into = streak % ACT_FREEZE_EVERY

        if streak == 0:
            body = (f"📉 *No activity streak yet*\n\n"
                    f"A *valid activity day* = ≥1 activity burning ≥{ACT_MIN_CALS} kcal\n"
                    f"Use `/workout` to start building yours.")
        elif streak < ACT_FREEZE_EVERY:
            body = (f"🏃 *Activity streak: {streak} day{'s' if streak>1 else ''}*\n\n"
                    f"{pct_bar(days_into, ACT_FREEZE_EVERY)} {days_into}/{ACT_FREEZE_EVERY} — "
                    f"{ACT_FREEZE_EVERY - days_into} more to earn a freeze ❄️")
        else:
            body = f"🏆 *Activity streak: {streak} days*\n\nConsistency!"

        body += f"\n\n❄️ *Freezes: {balance}/{ACT_FREEZE_MAX}*"
        if balance < ACT_FREEZE_MAX:
            if days_into == 0:
                body += f"\n{pct_bar(0, ACT_FREEZE_EVERY)} 0/{ACT_FREEZE_EVERY} — new period started"
            else:
                body += f"\n{pct_bar(days_into, ACT_FREEZE_EVERY)} {days_into}/{ACT_FREEZE_EVERY} to next freeze"

        await update.message.reply_text(body, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def actfreeze_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        await check_and_credit_act_freezes(update.effective_chat.id)
        streak, balance_str = await asyncio.gather(
            compute_activity_streak(), db_get_setting("act_freeze_balance"))
        balance = int(balance_str)
        days_into = streak % ACT_FREEZE_EVERY
        days_to_next = ACT_FREEZE_EVERY - days_into if days_into > 0 else ACT_FREEZE_EVERY

        lines = [
            f"❄️ *Activity freezes: {balance}/{ACT_FREEZE_MAX}*\n",
            f"*Activity streak:* {streak} day{'s' if streak!=1 else ''}",
            f"*Progress to next:* {pct_bar(days_into, ACT_FREEZE_EVERY)} {days_into}/{ACT_FREEZE_EVERY}",
            f"{days_to_next} more active day{'s' if days_to_next!=1 else ''} to earn one\n",
            f"*Valid activity day:* ≥1 activity burning ≥{ACT_MIN_CALS} kcal",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def delworkout_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        acts = await db_fetch_activities(today_str())
        if not acts:
            await update.message.reply_text("No activities logged today."); return
        if not ctx.args:
            await update.message.reply_text(
                numbered_activities_list(acts, "🏃 *Today's activities — /delworkout N to remove:*"),
                parse_mode="Markdown"); return
        try: n = int(ctx.args[0])
        except ValueError:
            await update.message.reply_text("Usage: `/delworkout N`", parse_mode="Markdown"); return
        if not (1 <= n <= len(acts)):
            await update.message.reply_text(f"No activity #{n}. Today has {len(acts)} logged."); return
        target = acts[n-1]
        await db_delete_activity(target["id"])
        icon = ACTIVITY_ICONS.get(target["activity"], "🏃")
        await update.message.reply_text(
            f"🗑 Removed #{n}: {icon} *{target['activity'].title()}*", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

# ── Nutrition edit/delete/undo/save/presets/log/copy/remind/ping ──
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
        try: n = int(args[0])
        except ValueError:
            await update.message.reply_text("Usage: `/edit N cal protein carbs fat fiber`", parse_mode="Markdown"); return
        if not (1 <= n <= len(meals)):
            await update.message.reply_text(f"No meal #{n}. Today has {len(meals)} meal(s)."); return
        m = meals[n-1]
        if len(args) == 1:
            lines = [f"✏️ *Meal #{n}: {m['name']}*\n",
                     f"Time: `{fmt12(m['meal_time'])}`",
                     f"Cal: `{round(m['cal'])}` · Pro: `{round(m['protein'])}g`",
                     f"Carbs: `{round(m['carbs'])}g` · Fat: `{round(m['fat'])}g` · Fiber: `{round(m.get('fiber',0))}g`\n",
                     f"_To update:_ `/edit {n} cal protein carbs fat fiber`"]
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown"); return
        keys = ["cal","protein","carbs","fat","fiber"]
        updates = {}
        for i, val_str in enumerate(args[1:6]):
            try: updates[keys[i]] = float(val_str)
            except ValueError:
                await update.message.reply_text(f"Invalid value: `{val_str}`", parse_mode="Markdown"); return
        await db_update(m["id"], updates)
        summary = " · ".join(f"{round(v)}{'kcal' if k=='cal' else 'g '+k}" for k,v in updates.items())
        await update.message.reply_text(f"✅ Updated #{n}: *{m['name']}*\n`{summary}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        meals = await db_fetch_today()
        if not meals:
            await update.message.reply_text("Nothing logged today to delete."); return
        if not ctx.args:
            await update.message.reply_text(
                numbered_meals_list(meals, "🗑 *Today's meals — /delete N to remove:*"),
                parse_mode="Markdown"); return
        try: n = int(ctx.args[0])
        except ValueError:
            await update.message.reply_text("Usage: `/delete N`", parse_mode="Markdown"); return
        if not (1 <= n <= len(meals)):
            await update.message.reply_text(f"No meal #{n}. Today has {len(meals)} logged."); return
        target = meals[n-1]
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
        await update.message.reply_text("Usage: `/save name cal protein carbs fat fiber`\ne.g. `/save whey 131 30 1 0.6 0`", parse_mode="Markdown"); return
    name = ctx.args[0].lower()
    vals = []
    for v in ctx.args[1:]:
        try: vals.append(float(v))
        except ValueError: pass
    if not vals:
        await update.message.reply_text("❓ No macros found. Format: `/save name cal protein carbs fat fiber`", parse_mode="Markdown"); return
    keys = ["cal","protein","carbs","fat","fiber"]
    macros = {keys[i]: vals[i] for i in range(min(len(vals),5))}
    if "cal" not in macros:
        await update.message.reply_text("Need at least calories.", parse_mode="Markdown"); return
    full = {k: macros.get(k,0) for k in keys}
    try:
        await db_save_preset(name, full)
        await update.message.reply_text(
            f"💾 Saved *{name}*\n"
            f"`{round(full['cal'])} kcal · {round(full['protein'])}g pro · "
            f"{round(full['carbs'])}g carbs · {round(full['fat'])}g fat · {round(full['fiber'])}g fiber`\n\n"
            f"Type `{name}` or `/log {name}` to log it.",
            parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}\n\n_Make sure the `presets` table exists._", parse_mode="Markdown")

async def presets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        presets = await db_list_presets()
        if not presets:
            await update.message.reply_text("No presets yet.\n\nSave one: `/save name cal protein carbs fat fiber`", parse_mode="Markdown"); return
        lines = ["💾 *Saved presets*\n"]
        for p in presets:
            lines.append(f"• *{p['name']}* — {round(p['cal'])} kcal · {round(p['protein'])}g pro · {round(p['carbs'])}g carbs · {round(p['fat'])}g fat · {round(p['fiber'])}g fiber")
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
        if not await db_fetch_preset(name):
            await update.message.reply_text(f"No preset named *{name}*.", parse_mode="Markdown"); return
        await db_delete_preset(name)
        await update.message.reply_text(f"🗑 Removed preset *{name}*.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def log_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        meal = {"meal_date": today_str(), "meal_time": now_time(), "name": preset["name"],
                "cal": preset["cal"], "protein": preset["protein"],
                "carbs": preset["carbs"], "fat": preset["fat"], "fiber": preset["fiber"]}
        await db_insert(meal)
        meals = await db_fetch_today()
        totals = {k: sum(float(m.get(k) or 0) for m in meals) for k in TARGETS}
        cal_pct = min(round((totals["cal"]/TARGETS["cal"])*100), 100)
        pro_pct = min(round((totals["protein"]/TARGETS["protein"])*100), 100)
        await update.message.reply_text(
            f"⚡ Logged: *{meal['name']}* `{fmt12(meal['meal_time'])}`\n"
            f"`{round(meal['cal'])} kcal · {round(meal['protein'])}g protein`\n\n"
            f"Today: {round(totals['cal'])}/{TARGETS['cal']} kcal ({cal_pct}%) · "
            f"{round(totals['protein'])}/{TARGETS['protein']}g pro ({pro_pct}%)",
            parse_mode="Markdown")
        asyncio.create_task(check_and_credit_freezes(update.effective_chat.id))
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def copy_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        yd = yesterday_str()
        meals = await db_fetch_date(yd)
        if not meals:
            await update.message.reply_text(f"Nothing logged {yd} to copy."); return
        lines = [f"📋 Copy {len(meals)} meal{'s' if len(meals)!=1 else ''} from yesterday to today?\n"]
        for m in meals:
            lines.append(f"• *{m['name']}* `{fmt12(m['meal_time'])}` — {round(m['cal'])} kcal")
        pending_id = str(uuid.uuid4())[:8]
        PENDING[pending_id] = {"action": "copy", "data": meals}
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Copy all", callback_data=f"copy:{pending_id}"),
            InlineKeyboardButton("❌ Cancel",   callback_data=f"cancel:{pending_id}")]])
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        if REMINDER:
            await update.message.reply_text(f"⏰ Reminder set for *{fmt12(REMINDER['time'])}* IST\nUse `/remind off` to disable.", parse_mode="Markdown")
        else:
            await update.message.reply_text("No reminder set.\n\nUsage: `/remind 9pm` or `/remind 21:00`", parse_mode="Markdown")
        return
    arg = " ".join(ctx.args).lower().strip()
    if arg == "off":
        REMINDER.clear(); await update.message.reply_text("⏰ Reminder disabled."); return
    t = parse_reminder_time(arg)
    if not t:
        await update.message.reply_text("❓ Couldn't parse time. Try `/remind 9pm`", parse_mode="Markdown"); return
    REMINDER["time"] = t; REMINDER["chat_id"] = update.effective_chat.id; REMINDER_SENT.clear()
    await update.message.reply_text(
        f"⏰ Reminder set for *{fmt12(t)}* IST\nUse `/remind off` to disable.", parse_mode="Markdown")

async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = await update.message.reply_text("⏳ Checking...")
    t0 = _time.time()
    try:
        await db_fetch_date(today_str())
        ms = round((_time.time()-t0)*1000)
        await msg.edit_text(f"✅ Online — DB responded in {ms}ms")
    except Exception as e:
        await msg.edit_text(f"⚠️ DB error: {e}")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text.strip()
    if not text: return

    # Check for preset match
    if len(text) <= 40 and not any(c.isdigit() for c in text):
        try:
            preset = await db_fetch_preset(text.strip().lower())
            if preset:
                meal = {"meal_date": today_str(), "meal_time": now_time(), "name": preset["name"],
                        "cal": preset["cal"], "protein": preset["protein"],
                        "carbs": preset["carbs"], "fat": preset["fat"], "fiber": preset["fiber"]}
                pending_id = str(uuid.uuid4())[:8]
                PENDING[pending_id] = {"action": "log", "data": meal}
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚡ Log it", callback_data=f"log:{pending_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{pending_id}")]])
                await update.message.reply_text(
                    f"⚡ *{meal['name']}* (preset)\n"
                    f"`{round(meal['cal'])} kcal  ·  {round(meal['protein'])}g protein`\n"
                    f"`{round(meal['carbs'])}g carbs  ·  {round(meal['fat'])}g fat  ·  {round(meal['fiber'])}g fiber`\n\n"
                    f"Log at *{fmt12(meal['meal_time'])}* today?",
                    parse_mode="Markdown", reply_markup=keyboard)
                return
        except Exception: pass

    meal, err = parse_message(text)
    if err:
        await update.message.reply_text(err, parse_mode="Markdown"); return
    pending_id = str(uuid.uuid4())[:8]
    PENDING[pending_id] = {"action": "log", "data": meal}
    dl = human_date(meal['meal_date'])
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Log it", callback_data=f"log:{pending_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{pending_id}")]])
    await update.message.reply_text(
        f"*{meal['name']}*\n"
        f"`{round(meal['cal'])} kcal  ·  {round(meal['protein'])}g protein`\n"
        f"`{round(meal['carbs'])}g carbs  ·  {round(meal['fat'])}g fat  ·  {round(meal['fiber'])}g fiber`\n\n"
        f"Log at *{fmt12(meal['meal_time'])}* on {dl}?",
        parse_mode="Markdown", reply_markup=keyboard)

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, pending_id = query.data.split(":", 1)

    if action == "cancel":
        PENDING.pop(pending_id, None)
        await query.edit_message_text("❌ Cancelled."); return

    pending = PENDING.pop(pending_id, None)
    if not pending:
        await query.edit_message_text("⚠️ Session expired. Please try again."); return

    if action == "log":
        meal = pending["data"]
        try:
            await db_insert(meal)
            meals = await db_fetch_today()
            totals = {k: sum(float(m.get(k) or 0) for m in meals) for k in TARGETS}
            cal_pct = min(round((totals["cal"]/TARGETS["cal"])*100), 100)
            pro_pct = min(round((totals["protein"]/TARGETS["protein"])*100), 100)
            await query.edit_message_text(
                f"✅ Logged: *{meal['name']}*\n"
                f"`{round(meal['cal'])} kcal · {round(meal['protein'])}g protein`\n\n"
                f"Today: {round(totals['cal'])}/{TARGETS['cal']} kcal ({cal_pct}%) · "
                f"{round(totals['protein'])}/{TARGETS['protein']}g pro ({pro_pct}%)",
                parse_mode="Markdown")
            asyncio.create_task(check_and_credit_freezes(query.message.chat_id))
        except Exception as e:
            await query.edit_message_text(f"⚠️ Failed to log: {e}")

    elif action == "workout":
        act = pending["data"]
        try:
            await db_insert_activity(act)
            asyncio.create_task(check_and_credit_act_freezes(query.message.chat_id))
            icon = ACTIVITY_ICONS.get(act["activity"], "🏃")
            valid = int(act["calories_burned"]) >= ACT_MIN_CALS
            badge = "✓ Counts toward activity streak" if valid else f"· {ACT_MIN_CALS - act['calories_burned']} kcal short of streak threshold"
            await query.edit_message_text(
                f"✅ Logged: {icon} *{act['activity'].title()}*\n"
                f"`{act['duration_minutes']} min · {act['intensity']} · {act['calories_burned']} kcal burned`\n\n"
                f"{badge}",
                parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"⚠️ Failed to log: {e}")

    elif action == "copy":
        meals_to_copy = pending["data"]
        today = today_str()
        try:
            count = 0
            for m in meals_to_copy:
                await db_insert({"meal_date": today, "meal_time": m["meal_time"], "name": m["name"],
                                 "cal": m["cal"], "protein": m["protein"],
                                 "carbs": m["carbs"], "fat": m["fat"], "fiber": m.get("fiber",0)})
                count += 1
            await query.edit_message_text(f"✅ Copied {count} meal{'s' if count!=1 else ''} from yesterday to today.")
        except Exception as e:
            await query.edit_message_text(f"⚠️ {e}")

    elif action == "cheatday":
        date = pending["data"]["date"]
        try:
            balance = int(await db_get_setting("freeze_balance"))
            if balance <= 0:
                await query.edit_message_text("❌ No freezes available."); return
            await db_add_cheat_day(date)
            await db_set_setting("freeze_balance", str(balance-1))
            await query.edit_message_text(
                f"❄️ Cheat day activated for *{human_date(date)}*\n\n"
                f"Streak protected · excluded from averages\n"
                f"Balance remaining: {balance-1}/{FREEZE_MAX}",
                parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"⚠️ {e}")

# ── Main ──────────────────────────────────────────────────────
async def main():
    global APP
    threading.Thread(target=run_health_server, daemon=True).start()
    print(f"Health server on port {PORT}")

    APP = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [
        ("start",      start),       ("help",       help_cmd),
        ("today",      today_cmd),   ("yesterday",  yesterday_cmd),
        ("week",       week_cmd),    ("streak",     streak_cmd),
        ("targets",    targets_cmd),
        ("freeze",     freeze_cmd),  ("cheatday",   cheatday_cmd),
        ("edit",       edit_cmd),    ("delete",     delete_cmd),
        ("undo",       undo_cmd),
        ("save",       save_cmd),    ("presets",    presets_cmd),
        ("unsave",     unsave_cmd),  ("log",        log_cmd),
        ("copy",       copy_cmd),    ("remind",     remind_cmd),
        ("ping",       ping_cmd),
        # Activity
        ("workout",    workout_cmd), ("activities", activities_cmd),
        ("actstreak",  actstreak_cmd),("actfreeze", actfreeze_cmd),
        ("delworkout", delworkout_cmd),
    ]:
        APP.add_handler(CommandHandler(cmd, fn))
    APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    APP.add_handler(CallbackQueryHandler(handle_callback))

    await APP.initialize()
    await APP.start()
    print("NutriTrack bot running...")
    await APP.updater.start_polling(drop_pending_updates=True)

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
