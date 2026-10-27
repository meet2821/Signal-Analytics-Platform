# Deployment guide: GitHub → Supabase → Render → Vercel

GitHub itself only stores the code and runs CI. It cannot host this app (GitHub Pages serves static
files only, and we need an API and a database). The standard flow is: **push to GitHub, and let Render
(API) and Vercel (frontend) deploy from the repo on every push.** All three have free tiers.

Order matters because each step produces a value the next one needs:

```
1 GitHub repo → 2 Supabase (DB + auth) → 3 Seed data → 4 Render (API) → 5 Vercel (UI) → 6 CORS → 7 Demo user
```

## 1. Put the code on GitHub

```bash
cd mono-edge-signals
git init -b main
git add .
git commit -m "Mono Edge signal analytics platform"
```

Create an **empty** repo on github.com (no README/license), then:

```bash
git remote add origin https://github.com/<your-user>/mono-edge-signals.git
git push -u origin main
```

(or with the GitHub CLI: `gh repo create mono-edge-signals --public --source=. --push`)

After the first `npm install` in `frontend/`, also commit the generated `frontend/package-lock.json`
for reproducible builds. CI starts automatically (Actions tab): backend tests plus a frontend build.

## 2. Supabase: database and authentication

1. supabase.com → **New project**. Pick a region near you; save the database password.
2. **SQL Editor → New query**, paste the whole of `db/schema.sql`, **Run**. (Safe to run twice.)
3. **Authentication → Sign In / Providers → Email**: for the demo, turn **Confirm email off** so reviewers can sign up and sign in instantly.
4. Collect four values (Project Settings):
   - **Project URL** `https://<ref>.supabase.co`
   - **Anon / publishable key** (API settings), safe for the browser
   - **Database connection string**: click **Connect → Session pooler**. It looks like
     `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`.
     Use the **session pooler**, not the "direct" string: direct is IPv6-only on the free tier, and Render's free tier cannot reach it.
   - **JWT secret** (API settings → JWT settings), only needed if your project still uses the legacy HS256 signing. New projects use asymmetric keys, which the API verifies automatically from `SUPABASE_URL`.

## 3. Load the replayable history

From your machine (any machine that can reach Supabase):

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # set DATABASE_URL to the session pooler string
python -m app.pipeline seed
```

Expected output: two lines like `RM-01: bronze=180,000 silver=3,606 alerts_written=1`. In the Supabase
**Table Editor** (schema `mill`) you should see 360,000 rows in `bronze_readings` and 2 rows in `gold_alerts`.

## 4. Deploy the API on Render

1. render.com → **New → Blueprint** → connect GitHub → pick the repo. Render reads `render.yaml`.
2. Fill the prompted secrets:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | session pooler string from step 2 |
   | `SUPABASE_URL` | `https://<ref>.supabase.co` |
   | `SUPABASE_JWT_SECRET` | legacy JWT secret, or leave empty on asymmetric-key projects |
   | `CORS_ORIGINS` | leave a placeholder for now; set it in step 6 |

   `INGEST_KEY` is generated for you; `LIVE_SIM=1` starts the built-in live feed for mill `RM-03`.
3. Deploy. When it is live, open `https://<service>.onrender.com/health` → `{"ok": true}` and
   `https://<service>.onrender.com/docs` for the API explorer.

Free-tier note: Render sleeps a free service after ~15 min without traffic, which pauses the live feed and
makes the first request slow (30–60 s). The included workflow `.github/workflows/keepalive.yml` pings
`/health` every 10 minutes. To enable it: GitHub repo → **Settings → Secrets and variables → Actions →
Variables → New variable** `API_URL` = your Render URL (no trailing slash). Before a review, open the app
once to wake the API.

## 5. Deploy the frontend on Vercel

1. vercel.com → **Add New → Project** → import the GitHub repo.
2. **Root Directory: `frontend`** (important). Framework preset: Next.js (auto-detected).
3. **Environment Variables**:

   | Name | Value |
   |---|---|
   | `NEXT_PUBLIC_API_URL` | `https://<service>.onrender.com` |
   | `NEXT_PUBLIC_SUPABASE_URL` | `https://<ref>.supabase.co` |
   | `NEXT_PUBLIC_SUPABASE_ANON_KEY` | the anon / publishable key |

4. **Deploy.** You get `https://<project>.vercel.app`. From now on, every push to `main` redeploys.

## 6. Allow the frontend to call the API (CORS)

Render → your service → **Environment** → set `CORS_ORIGINS` to your Vercel URL
(e.g. `https://mono-edge.vercel.app`, no trailing slash; comma-separate several). Save; Render restarts.

## 7. Create the reviewer account

Supabase → **Authentication → Users → Add user → Create new user**, email + password, tick
**Auto Confirm User**. Put the credentials in the README table at the top. (Reviewers can also use
**Sign up** on the login page because email confirmation is off.)

## Verification checklist

- [ ] `https://<api>/health` returns `{"ok": true}`
- [ ] Opening the Vercel URL redirects to `/login`; signing in lands on the dashboard
- [ ] **Live**: `RM-03 ● live`; the status badge flips to ALERT within ~5 minutes (cobble at 200 s of each cycle)
- [ ] Switch to `RM-01` → **Jump to before cobble** → alert banner appears at 10:07:30
- [ ] **Alerts** lists the incident; **Inspect** shows 6 charts with the shaded abnormal window and the normal-vs-abnormal table
- [ ] **History**: choose `RM-01`, vibration, *Around first alert* → normal vs abnormal table shows a large change
- [ ] `curl -X POST $API/api/ingest` without the key returns 401
- [ ] Calling `$API/api/alerts` without a token returns 401

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Network is unreachable` / timeout connecting to the DB | You used the direct (IPv6) connection string. Use the **session pooler** string. |
| `password authentication failed` | Wrong DB password, or the pooler user is not `postgres.<ref>`. Copy the string again from **Connect**. |
| Browser shows CORS error | `CORS_ORIGINS` on Render does not exactly match the Vercel origin (scheme, no trailing slash). |
| `401 Invalid token` on every call | `SUPABASE_URL` wrong or missing on Render, or a legacy project missing `SUPABASE_JWT_SECRET`. |
| Dashboard shows `(replay)` instead of live | API asleep or `LIVE_SIM` not `1`. Wake it (open `/health`), wait ~10 s. |
| Vercel build fails | Root Directory not set to `frontend`, or a missing `NEXT_PUBLIC_*` variable. Run `npm run build` locally to see the exact error. |
| Sign-up says "check your email" | Email confirmation is still on (step 2.3), or create the user manually (step 7). |
| Supabase database growing | Live retention runs every minute (raw 20 min, silver and alerts 24 h). Check the `LIVE_MILL` id matches the mill being fed. |

## Alternative: single-platform API on Vercel

The FastAPI app can also run on Vercel's Python runtime, but the built-in live loop needs a long-running
process, so Render (or Railway / Fly.io) is the better host for it.
