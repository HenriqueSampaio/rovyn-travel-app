# Moving Rovyn to Supabase (Postgres + Auth)

Rovyn runs in two modes, decided entirely by environment variables:

| | Database | Sign-in |
|---|---|---|
| **No env vars** (default) | SQLite at `backend/data/rovyn.db` | Built-in email/password |
| **`DATABASE_URL` set** | Postgres (Supabase) | — |
| **`SUPABASE_URL` + frontend keys set** | — | Supabase Auth: email/password, Google, Apple |

The two switches are independent: you can move the database first and auth
second. For a hosted demo you want both.

---

## 1. Create the Supabase project (~2 min)

1. Go to [supabase.com](https://supabase.com) → **New project**.
2. Pick any name (e.g. `rovyn`), a region near you, and set a **database
   password** — save it, you'll need it in the connection string.

## 2. Collect the three values

1. **Database connection string** — click **Connect** in the project's top
   bar → **Session pooler** → copy the URI. It looks like:
   `postgresql://postgres.abcdefgh:<PASSWORD>@aws-0-eu-west-1.pooler.supabase.com:5432/postgres`
   Replace `<PASSWORD>` with your database password. If the password has
   special characters (`@`, `#`, `%`…), URL-encode them.
   *(Session pooler, port 5432, is the right choice for Rovyn's connection
   pool; it also works from IPv4-only networks.)*
2. **Project URL** — Project Settings → **API** → Project URL
   (`https://<ref>.supabase.co`).
3. **Anon/publishable key** — same page, the `anon` `public` key (new
   projects may call it a *publishable* key, `sb_publishable_...` — either
   works).

## 3. Fill in the env files

`Rovyn/.env` (backend — gitignored):

```
DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
SUPABASE_URL=https://<ref>.supabase.co
```

`Rovyn/frontend/.env.local` (frontend — gitignored, create from
`frontend/.env.example`):

```
VITE_SUPABASE_URL=https://<ref>.supabase.co
VITE_SUPABASE_ANON_KEY=<anon or publishable key>
```

Restart both servers after editing (Vite only reads env files at startup).
The backend creates all tables automatically on first boot.

## 4. Auth settings in the dashboard

1. **Authentication → URL Configuration**:
   - **Site URL**: `http://localhost:5173` while developing; change to the
     hosted frontend URL when you deploy.
   - **Additional Redirect URLs**: add both while transitioning, e.g.
     `http://localhost:5173` and `https://your-demo.example`.
2. **Authentication → Sign In / Up**: Email is enabled by default.
   For a frictionless demo you can turn **Confirm email** off — sign-ups
   then work instantly without a mail round-trip. Leave it on if you prefer
   verified addresses.

## 5. Google sign-in (~10 min, free)

1. [Google Cloud Console](https://console.cloud.google.com) → create (or
   pick) a project → **APIs & Services → OAuth consent screen**: External,
   app name "Rovyn", your support email. Add yourself as a test user while
   the app is unpublished.
2. **Credentials → Create credentials → OAuth client ID → Web application**:
   - Authorized JavaScript origins: `http://localhost:5173` (add the
     hosted URL later)
   - Authorized redirect URIs: `https://<ref>.supabase.co/auth/v1/callback`
3. Copy the **Client ID** and **Client secret** into Supabase →
   **Authentication → Sign In / Up → Google** (enable, paste, save).

## 6. Apple sign-in (optional — needs the $99/yr Apple Developer Program)

Worth doing only when the demo goes public; Google + email covers testing.

1. In [developer.apple.com](https://developer.apple.com/account) →
   Certificates, Identifiers & Profiles → **Identifiers**: create a
   **Services ID** (e.g. `com.yourname.rovyn.signin`), enable *Sign in with
   Apple*, and configure it with the return URL
   `https://<ref>.supabase.co/auth/v1/callback`.
2. Under **Keys**, create a *Sign in with Apple* key; download the `.p8`
   file and note the Key ID and your Team ID.
3. Supabase → Authentication → Sign In / Up → **Apple**: enter the Services
   ID as Client ID and generate the client secret from the `.p8`
   (Supabase's field help links a generator; Apple secrets expire every 6
   months, so calendar a renewal).

Until Apple is enabled, its button on the sign-in page simply returns
"provider is not enabled" — harmless.

## 7. Optional: carry your local data over

Copies every table from `backend/data/rovyn.db` into Postgres (safe to
re-run; existing rows are skipped):

```bash
cd Rovyn
DATABASE_URL="postgresql://..." .venv/bin/python -m backend.scripts.migrate_to_postgres
```

Legacy passwords can't move into Supabase Auth (different hashing), but
anyone who signs in through Supabase with the same email is automatically
re-linked to their old trips on first request.

## Dev vs production sign-in (email verification)

Production (`main`, deployed) signs everyone in through Supabase Auth:
email + password with **Confirm email** on, plus Google/Apple. That's the
right bar for real users, but painful for testing — fake addresses can never
receive the verification mail.

For development, set in `Rovyn/.env`:

```
ALLOW_LEGACY_AUTH=1
```

This re-opens the app's built-in `/auth/signup` + `/auth/login` **alongside**
Supabase Auth: accounts are created instantly (any email, no verification),
and the sign-in page's email form switches to it automatically (it reads
`GET /auth/config`), with Google/Apple buttons still working. The backend
accepts both token kinds at once.

The switch lives in the environment, not in the branch — dev and main run
the same code, and a production deployment simply doesn't set the flag, so
only verified Supabase sign-ins exist there. Accounts created this way are
plain rows in `users`; if that person later signs in through Supabase with
the same email, their data is re-keyed to the Supabase identity
automatically.

## Performance: put the database near you

Every SQL statement travels to the Supabase region, so **round-trip time is
the whole ballgame**: from Brazil to a `us-west-2` project each query costs
~210 ms before the database does any work. Pick the region closest to you
(and your users) when creating the project — e.g. `sa-east-1` (São Paulo)
from Brazil — for ~10–30 ms round trips. A project's region can't be changed
later; to move, create a new project in the right region, update
`DATABASE_URL`/`SUPABASE_URL`/keys, and re-run the migration script (it's
idempotent). The backend also keeps warm pooled connections, batches queries
into pipelines, and caches token lookups, but physics still wins — keep the
database close.

## Notes

- **The anon key is public by design** — it only talks to Supabase Auth.
  All data access goes through the FastAPI backend over `DATABASE_URL`,
  which bypasses Supabase's Row Level Security; the dashboard will show
  tables as "unrestricted", which is expected in this architecture. Never
  expose the `service_role` key or the database password.
- **Older Supabase projects** (pre-2025) sign tokens with a shared HS256
  secret instead of publishing JWKS. If sign-ins fail with "can't verify
  HS256 tokens", copy Project Settings → API → **JWT Secret** into
  `SUPABASE_JWT_SECRET` in `Rovyn/.env`.
- **Password reset** isn't wired up yet (needs a small reset page +
  `resetPasswordForEmail`) — a good follow-up before the public demo.
