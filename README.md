# NutriTrack Telegram Bot

No AI layer — just send meal name + macros, bot confirms and logs to Supabase.

## Deploy on Render (free forever)

### 1. Create your Telegram bot
- Message @BotFather on Telegram → /newbot → copy the token

### 2. Push to GitHub
- Create a new repo, push this folder to it

### 3. Deploy on Render
- Go to render.com → New → Web Service
- Connect your GitHub repo
- Settings:
  - Environment: Python
  - Build command: pip install -r requirements.txt
  - Start command: python bot.py
  - Instance type: Free

### 4. Set environment variables in Render
TELEGRAM_TOKEN        = from BotFather
SUPABASE_URL          = https://uikwwjmzrrxdvwmyguwv.supabase.co
SUPABASE_KEY          = your supabase anon key
ALLOWED_TELEGRAM_USER = sorathedoraa

### 5. Deploy — find your bot on Telegram, send /start

## How to log

Send: `meal name  calories  protein  carbs  fat  fiber`

Examples:
  chicken salad 320 38 12 12 2
  whey protein 120 25 3 2 0
  2 boiled eggs 140 12 0 10 0
  oats 380 12p 60c 6f 4fb       ← tagged format also works

Claude gives you the macros after every recipe. Just copy the numbers here.

## Commands
/today  — full day log + progress bars vs targets
/undo   — remove last logged meal
/start  — show help
