# NutriTrack

A personal fitness and nutrition tracker built as a single HTML file, deployed at [nutribot.explore-soraa.workers.dev](https://nutribot.explore-soraa.workers.dev).

## What it does

NutriTrack helps you log and visualise what you eat, how you move, and how your body changes — with an AI coach that understands natural language.

### Modules

**Nutrition**
- Log meals manually or by pasting a text line (`Chicken bhurji 2 dosa 400 27 41 15 2`)
- Tracks calories, protein, carbs, fat, and fibre against daily targets
- Streak tracking, cheat day system, monthly calendar heatmap
- Preset meals for one-tap re-logging

**Activity**
- Log workouts with type, duration, and intensity
- Calculates calories burned, tracks weekly active minutes
- Streak and cheat day system, activity history calendar

**Body**
- Log body weight
- Animated body figure reacts to logged activity (runs, cycles, lifts, swims, etc.)
- Body streak tracking

**Log tab**
- Unified quick-entry for meals, activities, body sets, and daily steps

**Summary**
- At-a-glance rings for calories, protein, steps, and activity
- Daily + historical view, day-detail modal

**Coach (AI)**
- Chat with a Gemini-powered fitness coach
- Logs meals directly from natural language ("I had 3 eggs and toast")
- Updates macro targets on request
- Logs body weight from chat
- Full conversation context maintained within session

## Stack

| Layer | Tech |
|---|---|
| Frontend | Single `index.html` — vanilla React 18 via CDN, no build step |
| Backend | Supabase (PostgreSQL + PostgREST REST API) |
| Auth | Supabase Auth — Google OAuth + email/password |
| AI | Google Gemini 2.0 Flash via Supabase Edge Function (Deno) |
| Hosting | Cloudflare Pages |

## Multi-user

Full Row Level Security — every user sees only their own data. Auth state: sign in → onboarding (pick modules) → app. Existing data auto-migrated on first login.
