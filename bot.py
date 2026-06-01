import os
import re
import json
import httpx
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ── Config ────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
ALLOWED_USER   = os.environ.get("ALLOWED_TELEGRAM_USER", "")

SUPABASE_API   = f"{SUPABASE_URL}/rest/v1/meals"
HEADERS        = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
TARGETS        = {"cal": 1850, "protein": 145, "carbs": 160, "fat": 55, "fiber": 30}

# ── Helpers ───────────────────────────────────────────────────
def ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))

def today_str():
    return ist_now().strftime("%Y-%m-%d")

def now_time():
    return ist_now().strftime("%H:%M")

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
    tagged = re.findall(r'(\d+\.?\d*)\s*(kcal|cal|p|pro|protein|c|carb|carbs|f|fat|fb|fiber|fibre)?', text, re.IGNORECASE)
    nums, plain = {}, []
    for val, tag in tagged:
        val = float(val)
        t = tag.lower() if tag else ""
        if t in ("kcal","cal"):              nums["cal"] = val
        elif t in ("p","pro","protein"):     nums["protein"] = val
        elif t in ("c","carb","carbs"):      nums["carbs"] = val
        elif t in ("f","fat"):               nums["fat"] = val
        elif t in ("fb","fiber","fibre"):    nums["fiber"] = val
        elif not t:                          plain.append(val)
    for key in ["cal","protein","carbs","fat","fiber"]:
        if key not in nums and plain:
            nums[key] = plain.pop(0)
    if not nums or "cal" not in nums:
        return None, "❓ Couldn't find macros. Format:\n`meal name 320 38 12 12 2`\n_(name cal protein carbs fat fiber)_"
    name_part = re.sub(r'\b\d+\.?\d*\s*(kcal|cal|p|pro|protein|c|carb|carbs|f|fat|fb|fiber|fibre)?\b', '', text, flags=re.IGNORECASE)
    name = re.sub(r'\s+', ' ', name_part).strip().strip('-').strip() or "Meal"
    return {"meal_date": today_str(), "meal_time": now_time(), "name": name[:80],
            "cal": nums.get("cal",0), "protein": nums.get("protein",0),
            "carbs": nums.get("carbs",0), "fat": nums.get("fat",0), "fiber": nums.get("fiber",0)}, None

# ── Handlers ──────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "👋 *NutriTrack Bot*\n\n"
        "Log meals by sending:\n`meal name  cal  protein  carbs  fat  fiber`\n\n"
        "*Examples:*\n"
        "`chicken salad 320 38 12 12 2`\n"
        "`whey protein 120 25 3 2 0`\n"
        "`oats banana 380 12p 60c 6f 4fb`\n\n"
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
    summary = (
        f"*{meal['name']}*\n"
        f"`{round(meal['cal'])} kcal  ·  {round(meal['protein'])}g protein`\n"
        f"`{round(meal['carbs'])}g carbs  ·  {round(meal['fat'])}g fat  ·  {round(meal['fiber'])}g fiber`\n\n"
        f"Log at {meal['meal_time']}?"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Log it", callback_data=json.dumps(meal)),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel")
    ]])
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=keyboard)

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled."); return
    try:
        meal = json.loads(query.data)
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
        await query.edit_message_text(f"⚠️ Failed: {e}")

# ── Main — manual lifecycle, compatible with Python 3.14 ──────
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("NutriTrack bot starting...")
    await app.initialize()
    await app.start()
    print("NutriTrack bot running. Polling for updates...")
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep running until interrupted
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        print("Shutting down...")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
