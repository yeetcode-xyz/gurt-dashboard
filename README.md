# gurt admin dashboard

A Streamlit app over the [gurt](https://github.com/yeetcode-xyz/gurt) store. Two
jobs: show what gurt is doing, and surface the cases where it got something
wrong.

**This repository is public. The data it reads is not.** See
[Security](#security) before deploying it anywhere.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

export DASHBOARD_PASSWORD='...'          # required — the app refuses to start
export DATABASE_URL='postgresql://...'
.venv/bin/streamlit run app.py           # → http://localhost:8501
```

Or copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill
it in. That file is gitignored.

## Security

The code here is public; everything that makes it dangerous is configuration.

**A password is mandatory and the app fails closed without one.** Streamlit
Community Cloud gives an app the default visibility of its repository — private
repo, private app; **public repo, public app**. The viewer allowlist still
exists and you should still use it, but it became something you must remember
to switch on. This page lists every customer's repository names, package
versions and migration history, so the cost of forgetting is that all of it is
on the open internet and indexable. [auth.py](auth.py) is the lock that does not
depend on remembering: no `DASHBOARD_PASSWORD`, no app. Use both.

**Use a `SELECT`-only database role.** The session is opened with
`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`, so a write fails even
against a superuser account — but that stops *the dashboard* writing, not
whoever obtains the credential:

```sql
CREATE ROLE gurt_readonly LOGIN PASSWORD '…';
GRANT CONNECT ON DATABASE postgres TO gurt_readonly;
GRANT USAGE ON SCHEMA public TO gurt_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO gurt_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO gurt_readonly;
```

That last line is the one people skip: without it the next migration adds a
table the dashboard cannot read, and it surfaces as an empty panel rather than
an error.

**Nothing secret is in this repository.** `certs/supabase-prod-ca-2021.crt` is
Supabase's public root certificate — a certificate, not a key — vendored because
it is not in the system trust store and a verifying connection fails without it.

## What it reads

**Postgres only** — all 15 tables. `GURT_STATE_DIR` (the job journal) is
deliberately out of scope: it is JSON on whichever machine ran the job.

Two of those tables are new and are caches rather than history: `registry_fact`
(what npm/PyPI said a package's latest version is) and `probe_result` (what a
live endpoint answered). They moved out of `GURT_KNOWLEDGE_DIR` and into the
database in Aug 2026, because a disk cache does not exist on a deployed instance
that has no disk — which is to say it had never once been used in production.
`spec_document` joins them: a provider's OpenAPI contract, kept **only** so the
next fetch can be a conditional request. All three are fleet-wide, hold no
repository data, and are safe to truncate — the next run simply re-asks.

Remote connections are verified against the vendored CA in `certs/`. Local
connections skip TLS, because a container Postgres does not speak it. `sslmode`
in the URL is stripped for remote hosts so a pasted `sslmode=require` cannot
silently turn verification off — that setting encrypts while accepting *any*
certificate, and it is the one thing that must not be decided by copy-paste.

## What counts as a mistake

The whole point of the alerting is the split between *gurt was wrong* and *gurt
correctly refused*, so it is worth being explicit. Defined in
[alerts.py](alerts.py).

**🔴 Harm — gurt shipped something and it was wrong:**

| signal | meaning |
| --- | --- |
| `merge_outcome = 'reverted'` | a human undid a merged change |
| `post_merge_ci = 'failed'` | merged, then *their* CI went red |
| `human_edited` on a merge | someone had to repair the branch first |
| `codemod.status = 'needs_review'` | a rollback demoted it, fleet-wide |
| `outcome = 'error'` | gurt itself crashed — always a bug |

**🟡 Not mistakes, and never counted as such:**

- `outcome IN ('red','blocked')` — gurt tried, validation refused, **no PR was
  opened**. That is the gate working. If this counted as harm, the safest
  possible run would look like the worst one.
- `merge_outcome = 'closed'` — the maintainer did not want the upgrade. It says
  nothing about whether the transform was correct, which is exactly why
  `applyOutcome` moves no counter for a close.
- `confirmed = false` failures — gurt could not attribute a batch failure and
  declined to guess. It suppresses nothing and will be retried.

These are still displayed, as counts. An empty mistakes panel is only
reassuring if you can also see that gurt is running and refusing things.

**🟠 Decay — gurt quietly doing less than it looks like it is:** retired spec
URLs, drift found repeatedly and never fixed, mapper misses piling up, codemods
stuck at `candidate`, suspended installations, and "the builder has never run".

## What it cannot tell you

**There is no "gurt ran and all was well" record.** Every table is an event log
of changes and problems:

- `recordSnapshot` returns early when the inventory is unchanged;
- `recordApiCheck` only fires for hosts *with* drift;
- `up-to-date` outcomes are skipped before recording.

So "last recorded activity" means *the last time something happened here*, and
a stale timestamp is ambiguous between **gurt stopped running** and **nothing
has changed** — the two states you most need to tell apart. The dashboard
labels it accordingly and warns in place; it does not pretend to know.

Fixing this properly needs a heartbeat row written on every run in `runner.ts`
— one insert per run, and then liveness becomes a real answer rather than a
prompt to go and look.

## Testing it

The schema belongs to the [gurt](https://github.com/yeetcode-xyz/gurt) repo, so
creating a test database means running its migrator once:

```bash
docker run -d --name gurt-pg-test -e POSTGRES_PASSWORD=gurt \
  -e POSTGRES_DB=gurt_test -p 55432:5432 postgres:16-alpine

# in a checkout of yeetcode-xyz/gurt
DATABASE_URL=postgres://postgres:gurt@localhost:55432/gurt_test \
  npm run app -- migrate

# back here
DATABASE_URL=postgres://postgres:gurt@localhost:55432/gurt_test \
  .venv/bin/python seed_demo.py
```

[seed_demo.py](seed_demo.py) writes one row of every alert class — *including*
the ones that must not alert — so the harm/refusal split can be checked rather
than assumed. It refuses to run against anything whose URL mentions `supabase`
or `pooler`.

## Hosting

**Streamlit Community Cloud**, free. Main file `app.py`, Python 3.12.
`_secret()` reads `st.secrets` before the environment, and `ca_path()` resolves
the vendored CA from `__file__`, so it works wherever the repo is checked out.

Put both of these in the app's Secrets box:

```toml
DASHBOARD_PASSWORD = "..."
DATABASE_URL = "postgresql://gurt_readonly:..."
```

Then **set the app's viewers explicitly** — Settings → Sharing → *"Only specific
people can view this app"*. Because this repo is public the app deploys public
by default, so this is a change you have to make, not one you inherit. The
password is the backstop for the day someone forgets; it is not a reason to skip
this step.

The database credential lives on Streamlit's infrastructure either way, which is
the argument for the `SELECT`-only role above being done first rather than
eventually.

There is no Dockerfile here on purpose: Streamlit Cloud builds from
`requirements.txt`, so a container image would be a second thing to keep in step
with no one running it.
