# Moving the database to Supabase (Postgres)

The app uses SQLAlchemy, so switching from SQLite to Supabase is a
**connection-string change** — no code changes. The schema in
[`supabase_schema.sql`](supabase_schema.sql) matches the models exactly.

## Steps

1. **Create a Supabase project** at supabase.com (pick a region close to you).
2. **Create the schema:** open **SQL Editor** → paste the contents of
   `docs/supabase_schema.sql` → **Run**. (Creates all tables + indexes.)
3. **Install the Postgres driver:**
   ```powershell
   pip install "psycopg[binary]"
   ```
   (Already listed in `requirements.txt`.)
4. **Get the connection string:** Supabase → **Project Settings → Database →
   Connection string**. Use the **Session pooler** string (works over IPv4,
   which most Windows/home networks need). It looks like:
   ```
   postgresql://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```
5. **Set `DATABASE_URL` in `.env`** with the `+psycopg` driver prefix:
   ```
   DATABASE_URL=postgresql+psycopg://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```
   (Direct connection on port `5432` — `db.<ref>.supabase.co` — also works but is
   IPv6-only on Supabase; prefer the pooler unless you have IPv6.)
6. **Start the app.** It now reads/writes Supabase. `init_db()` sees the tables
   already exist and does nothing; the SQLite-only WAL/migration code is skipped
   automatically for Postgres.

## What does NOT transfer
Moving to Supabase is a fresh database — existing **local** SQLite detections
stay in `data/media_monitoring.db`. Keywords/channels are quick to re-add via the
UI. (If you need the history migrated, say so and I'll write a one-off copy
script.)

## Credentials I need from you

For the app's **direct Postgres** connection, all I need is the **connection
string** (which embeds everything):

| Item | Where in Supabase |
|---|---|
| **Database password** | set at project creation; reset any time in **Settings → Database → Reset database password** |
| **Project ref + region** | already inside the connection string (`postgres.<ref>@aws-0-<region>...`) |

So: **paste me the Session-pooler connection string with the password filled in**
(or the password + I'll assemble it from your project ref/region).

**Not needed** for this app: the `anon` key, the `service_role` key, and the
Project URL (`https://<ref>.supabase.co`). Those are for Supabase's REST/JS
client and PostgREST — we connect straight to Postgres via SQLAlchemy, so they're
irrelevant here. Keep the `service_role` key secret regardless.

> Security: the connection string contains your DB password — treat it like any
> secret. It goes only in `.env` (git-ignored), never in code.
