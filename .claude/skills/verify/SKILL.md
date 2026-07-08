---
name: verify
description: Run Rovyn locally (isolated) to verify frontend/backend changes end-to-end — build, launch on side ports, seed a user + trips, drive with Playwright.
---

# Verifying Rovyn changes locally

## ⚠️ The .env trap (read first)

`backend/main.py` calls `load_dotenv()`, which **walks up parent directories**.
From a git worktree under `.claude/worktrees/`, it finds the main checkout's
`.env` — which sets `DATABASE_URL`/`SUPABASE_URL`, so your "local" backend
silently writes to the **real Supabase Postgres**. Always launch test backends
with explicit empty overrides (env vars beat `.env` since `load_dotenv` never
overrides existing vars):

```bash
DATABASE_URL="" SUPABASE_URL="" SUPABASE_JWT_SECRET="" ANTHROPIC_API_KEY="" \
  .venv/bin/uvicorn backend.main:app --port 8010
```

That forces per-checkout SQLite (`backend/data/rovyn.db`, gitignored) and
legacy auth (`/auth/config` must return `{"supabase": false, "legacy": true}`
— if `supabase` is `true`, you are pointed at the real DB, stop).

## Launch (side ports, won't collide with a dev session on 8000/5173)

```bash
# backend — from repo/worktree root; .venv lives in the MAIN checkout
DATABASE_URL="" SUPABASE_URL="" SUPABASE_JWT_SECRET="" ANTHROPIC_API_KEY="" \
  /path/to/main-checkout/.venv/bin/uvicorn backend.main:app --port 8010 &

# frontend — VITE_API_BASE points it at the test backend
cd frontend
ln -sfn /path/to/main-checkout/frontend/node_modules node_modules   # worktrees only
VITE_API_BASE=http://localhost:8010 npm run dev -- --port 5183 --strictPort &
```

Quick build check without launching: `./node_modules/.bin/esbuild --loader:.jsx=jsx <file> --outfile=/dev/null`.

## Seed data

```bash
TOKEN=$(curl -s -X POST localhost:8010/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"verify@example.com","password":"verify123","name":"Verifier"}' | jq -r .token)
# email validator rejects reserved TLDs like .local — use example.com
curl -s -X POST localhost:8010/trips -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"Summer in Hvar","destination":"Hvar, Croatia","solo":false}'
# trip statuses: waiting → voting → selected → curating → final_review → approved → booked
sqlite3 backend/data/rovyn.db "UPDATE trips SET status='booked' WHERE name='...';"
```

## Drive the UI

Playwright lives in the npx cache (`find ~/.npm/_npx -maxdepth 4 -name playwright -type d`);
run scripts with `NODE_PATH=<that dir> node script.js`. Chromium is already in
`~/Library/Caches/ms-playwright`. Sign in by injecting localStorage before load:

```js
await page.addInitScript(([t]) => {
  localStorage.setItem('rovyn_token', t);
  localStorage.setItem('rovyn_user', JSON.stringify({ id: 'x', email: 'verify@example.com', name: 'Verifier' }));
}, [TOKEN]);
await page.goto('http://localhost:5183/trips');
```

## Teardown

Kill both servers; in a worktree also remove `frontend/node_modules` (symlink),
`frontend/dist`, and `backend/data/rovyn.db` — all gitignored, but keep it tidy.
