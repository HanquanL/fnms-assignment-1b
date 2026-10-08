# Home Base — FNMS Assignments 1 & 1B

A personal "home base" web app (A1: accounts, login, account management) and its first tool (1B): an
**agentic tracker** that finds the top 5 **open-weight AI model releases**, ranks them, summarizes them with
sources, and on every later run reports what's new since the last one. Results appear on the app's
**Tracker** page.

FastAPI + Postgres (Neon) backend, React + Vite frontend, and a hand-written agent loop in Python
(Gemini for the model, Tavily for search). The design questions are answered in [AGENT.md](AGENT.md).

## Tech stack

| Layer    | Choice |
|----------|--------|
| Database | PostgreSQL on [Neon](https://neon.com) (hosted, nothing to install) |
| Backend  | Python 3.11, FastAPI, SQLAlchemy 2, psycopg 3 |
| Auth     | Hand-written: argon2id password hashing (`argon2-cffi`), HS256 JWT bearer tokens (`PyJWT`) |
| Frontend | React 19, Vite, Tailwind CSS v4, React Router, lucide-react |
| Tracker  | Own agent loop (no agent framework), `httpx` for every HTTP call, Gemini REST API (`gemini-3.5-flash-lite`), Tavily search, BeautifulSoup text extraction |

## Prerequisites

- **Python 3.11+**
- **Node.js 22.12+** (or 20.19+) — required by Vite
- **Git**
- For the tracker, two free API keys (no credit card for either):
  - **Gemini**: <https://aistudio.google.com/api-keys>. Don't enable billing: on the free tier the spend cap is $0.
  - **Tavily**: <https://app.tavily.com> (1,000 free credits a month).
- No local database: the app uses a hosted Neon Postgres database whose connection string is in `backend/.env`.

## Quick start

Open **three terminals** in the repository root: backend, frontend, tracker. The commands below are in the
order to run them. Windows PowerShell first; macOS / Linux equivalents follow each block.

### 1. Backend — terminal 1

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python seed.py
uvicorn app.main:app --port 8000
```
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python seed.py
uvicorn app.main:app --port 8000
```

> If PowerShell refuses to run `Activate.ps1`, skip activation and call the venv's Python directly, e.g.
> `.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`.

Check it's up: <http://localhost:8000/healthz> → `{"status":"ok"}`. API docs: <http://localhost:8000/docs>.

### 2. Frontend — terminal 2

```bash
cd frontend
npm ci
npm run dev
```

Open **<http://localhost:5173>** and sign in:

| Username    | Password       |
|-------------|----------------|
| `NYUgrader` | `Courant2026!` |

`python seed.py` (step 1) recreates this account if it was deleted; `python seed.py --reset` restores its
password if it was changed. The **Tracker** link in the top bar shows the tracker's reports.

### 3. Tracker setup — terminal 3 (repository root)

The tracker reuses the backend's virtual environment (its three extra packages don't conflict).

```powershell
backend\.venv\Scripts\Activate.ps1
python -m pip install -r tracker\requirements.txt
Copy-Item .env.example .env
```
```bash
source backend/.venv/bin/activate
pip install -r tracker/requirements.txt
cp .env.example .env
```

Edit the new **`.env`** in the repository root and fill in all four values:

```env
GEMINI_API_KEY=<your Gemini key>
TAVILY_API_KEY=<your Tavily key>
TRACKER_USERNAME=NYUgrader
TRACKER_PASSWORD=Courant2026!
```

The tracker logs in to the backend as this user, and its runs show up on that user's Tracker page.
Check the keys before the first run (uses 1 Tavily credit and one tiny Gemini call):

```bash
python tests/keys_check.py
```

### 4. Run the tracker

The backend (step 1) must be running. From the repository root, with the venv active:

```bash
python -m tracker run --label run1
```

It takes 1–4 minutes (most of it waiting for the model). It prints the status, the developments found,
and where it wrote `reports/run1.md` and `traces/run1.jsonl`, then saves the run to the backend. Refresh the
Tracker page to see it.

### 5. Run it again

```bash
python -m tracker run --label run2
```

This run loads what run 1 left behind: it skips articles it already read, recognizes a new article about a
known development, and organizes its report as **New since last run**, **Still in top K**, and **Dropped**.
(The committed `reports/run2.md` was made at least a day after `run1.md`, as the assignment asks.)

### 6. Reset its saved state

```bash
python -m tracker reset
```

It asks before deleting (add `--yes` to skip the question). This removes the user's saved runs,
developments, and seen URLs; the next run starts with no memory.

### Other tracker commands

| Command | What it does |
|---------|--------------|
| `python -m tracker status` | What the tracker remembers: seen URLs, known developments, last top K |
| `python -m tracker run --local` | Development run: no memory, no backend; writes to `reports/dev/` and `traces/dev/` (git-ignored) |
| `python -m tracker save reports/unsaved-<label>.json` | Saves a finished run that the backend couldn't accept at the time (the backend was down) |
| `python -m tracker.tools fetch_article <url>` | The fetch tool on its own, through all guardrails. Exit code 0 fetched, 2 rejected, 1 failed |
| `python -m tracker.tools search_web "<query>"` | The search tool on its own (1 Tavily credit) |
| `python -m tracker.tools finish <report.json>` | Validates a report file's structure (provenance needs a live run) |

Exit codes for `python -m tracker run`: 0 complete, 2 partial (a budget ran out or a terminal error after
some evidence was collected), 1 failed or stopped before starting (with the reason printed).

## How the tracker works

```
config.yaml (policy, read-only)        A1 backend (memory)                        Neon
        |                         login -> GET /api/tracker/state  --->  backend  ---> Postgres
        v                                          ^
  agent loop (tracker/agent.py) ------------------- POST /api/tracker/runs at the end
     | model call: Gemini (what to do next)
     | search_web: Tavily
     | fetch_article: any public web page, through the guardrails
     v
  finish(report) -> validated claim by claim -> reports/<label>.md + traces/<label>.jsonl
```

- **Policy lives in `config.yaml`**: topic, K, model and fallback model, instructions, tools, limits, allowed
  schemes / ports / hosts, search settings, retry settings. It is loaded once and frozen.
- **Budgets are enforced by the runtime**, not requested from the model: model calls, searches, fetches,
  tokens (including thinking tokens), and wall-clock time. When a run limit is hit, one held-back model call
  asks for a final report, and the run is saved as **partial** with whatever verified evidence exists.
- **Failures are classified.** Timeouts, network errors, 5xx, and per-minute 429s are retried with capped
  exponential backoff. A bad key, an exhausted daily or monthly quota, or payment required stops the run at
  once with a message saying what to do. Model calls are paced client-side to stay under the per-minute limit.
- **`fetch_article` guardrails**, before any request: only `http`/`https`, only ports 80/443, no credentials in
  the URL, and every address the host resolves to must be public (no loopback, private, link-local such as
  `169.254.169.254`, multicast, reserved, or IPv4 hidden in IPv6). The connection is pinned to the checked IP
  (so DNS can't change its answer in between), every redirect is checked again, and there are timeouts plus
  a 2 MB size cap.
- **Web text is data.** Tool results reach the model inside `<untrusted_web_content>` fences it can't close
  early, and the instructions, tools, and budget aren't in the conversation at all, so a page can't change them.
- **Provenance is checked claim by claim.** Every development is 1–4 claims, each with the URL of an article
  the run actually fetched and a sentence copied from it. The code rejects a claim whose sentence isn't in that
  article, or that mentions a number the article never states, and tells the model what to fix.
- **Memory** is stored in the A1 database through the A1 API (the tracker never touches the database).

## Environment

`backend/.env` and `frontend/.env` are **committed on purpose** so the project runs as-is. They contain only
a throwaway Neon database created for this course and a random JWT secret used nowhere else. The repository
root `.env` holds the tracker's API keys and is **git-ignored**; `.env.example` lists its variables.

**`.env` (repository root, not committed)**

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEY` | Gemini API key (free tier) |
| `TAVILY_API_KEY` | Tavily API key (free tier) |
| `TRACKER_USERNAME`, `TRACKER_PASSWORD` | The A1 account the tracker logs in as (`NYUgrader` / `Courant2026!`, published in the assignment) |
| `TRACKER_API_URL` | Optional. Backend URL; defaults to `backend.api_url` in `config.yaml` (`http://localhost:8000`) |

**`backend/.env`**

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Postgres connection string (SQLAlchemy format: `postgresql+psycopg://...`) |
| `JWT_SECRET` | HMAC key used to sign tokens |
| `JWT_ALGORITHM` | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime (60) |
| `CORS_ORIGINS` | Comma-separated frontend origins allowed to call the API |

**`frontend/.env`**

| Variable | Purpose |
|----------|---------|
| `VITE_API_URL` | Backend base URL (`http://localhost:8000`). Anything prefixed `VITE_` ships to the browser, so no secrets here. |

## Database & migrations

No migration command is needed. On startup the backend runs SQLAlchemy `create_all`, which creates any
missing table and never modifies or drops existing data. Data lives in Neon, so it survives restarts.

### Schema

Every tracker row belongs to a user, and deleting the user (A1 "delete account") removes all of it through
`ON DELETE CASCADE` in the database.

| Table | Columns | Role |
|-------|---------|------|
| `users` | `id` UUID PK, `username` (unique, case-insensitive), `email` (unique), `password_hash`, `created_at`, `updated_at` | A1 accounts |
| `tracker_runs` | `id` UUID PK, `user_id` FK, `started_at`, `finished_at`, `status` (complete / partial / failed), `stop_reason`, `topic`, `k`, `model`, `stats` JSON, `report_md` | One row per run |
| `tracker_articles` | `id` PK, `run_id` FK, `user_id` FK, `url`, `canonical_url` (indexed with `user_id`), `title`, `status` (fetched / skipped / rejected / failed), `reason`, `http_status`, `fetched_at` | Every URL a run touched. "Already seen" = any earlier article with status `fetched` |
| `tracker_developments` | `id` UUID PK, `user_id` FK, `key` (unique per user, e.g. `qwen/qwen-4/release`), `title`, `summary`, `first_seen_run_id`, `last_seen_run_id`, `created_at`, `updated_at` | A real-world development, independent of the articles that reported it |
| `tracker_sources` | `id` PK, `development_id` FK, `url` (unique per development), `title`, `evidence`, `first_run_id` | The articles supporting a development, with the quoted evidence |
| `tracker_rankings` | `id` PK, `run_id` FK, `development_id` FK, `rank` (null when dropped), `change` (new / still / dropped), `summary` (as reported in that run) | Each run's top-K snapshot, including what dropped |

Why this shape: a development and an article are different things (one development, many articles), so
"a new URL reports a development we already covered" is just one more `tracker_sources` row on an existing
development, and the unique `(user_id, key)` constraint makes duplicates impossible. Saving a run is a single
transaction: all of its rows are written, or none are.

## API

JSON in, JSON out. Protected routes need `Authorization: Bearer <token>`.

| Method | Path | Auth | Success | Purpose |
|--------|------|------|---------|---------|
| GET    | `/healthz` | no | 200 | `{"status":"ok"}` |
| POST   | `/api/auth/register` | no | 201 | Create an account: `{"username", "password", "email"?}` → user |
| POST   | `/api/auth/login` | no | 200 | `{"username" or "email", "password"}` → `{"access_token", "token_type", "expires_in", "user"}` |
| GET    | `/api/auth/me` | yes | 200 | The logged-in user |
| GET    | `/api/users/:id` | yes | 200 | Read your user |
| PATCH  | `/api/users/:id` | yes | 200 | Update `username`, `email`, or `password` (password change requires `current_password`) |
| DELETE | `/api/users/:id` | yes | 204 | Delete your account |
| GET    | `/api/tracker/state` | yes | 200 | Tracker memory: `seen_urls`, `developments` (with sources), `last_run_id`, `last_top_k` |
| POST   | `/api/tracker/runs` | yes | 201 | Save one finished run: articles, developments, rankings, report, stats (one transaction) |
| DELETE | `/api/tracker/state` | yes | 204 | Reset: delete the user's runs, developments, and seen URLs |
| GET    | `/api/tracker/runs` | yes | 200 | Run history, newest first, with new / still / dropped and article-status counts |
| GET    | `/api/tracker/runs/latest` | yes | 200 | The latest non-failed run: rankings with sources, and its articles (404 if none) |
| GET    | `/api/tracker/runs/:id` | yes | 200 | One run in the same shape |

A user is always returned as `{id, username, email, created_at, updated_at}`.

**Errors:** 401 missing/invalid/expired token or bad login · 404 not your account or run (see below) ·
409 username/email taken · 400 wrong current password · 422 invalid input.

**The three rules**
1. **No password hash ever leaves the API.** Every route serializes users through one response model
   (`UserOut`) that has no password field.
2. **No token, bad token, or expired token → 401.** One dependency (`get_current_user`) handles every case,
   for the tracker endpoints too.
3. **You can't touch another user's data → 404.** GET, PATCH, and DELETE on a user `:id` that isn't yours, and
   GET on another user's tracker run, return the same 404 as an id that doesn't exist (or isn't a valid UUID).
   *Why 404 and not 403:* a 403 confirms that the thing exists, which lets an attacker enumerate valid ids.
   With 404, "someone else's" and "doesn't exist" are indistinguishable. (GitHub does the same for private
   repositories.) Auth is still checked first, so a missing token is 401, not 404. The tracker endpoints have
   no user id in the path at all: every query is filtered by the token's user.

**Web text in the app.** Everything the tracker found on the web (titles, summaries, evidence, URLs, reasons)
is rendered by React as text, never as HTML: no `dangerouslySetInnerHTML`, no Markdown-to-HTML. Only `http(s)`
URLs become links, and a URL a guardrail rejected is never a link, even if it is `http`.

## Tests

From the repository root with the venv active (`backend\.venv\Scripts\Activate.ps1`). None of them call the
model; the ones marked "backend" need it running and use throwaway users they delete afterwards.

| Command | Checks |
|---------|--------|
| `python tests/api_check.py` | A1 API: every endpoint, shapes, status codes, the three rules (68 checks, backend) |
| `python tests/tracker_api_check.py` | Tracker endpoints: 401s, validation, atomic saves, dedup, isolation between users, reset (backend) |
| `python tests/guard_check.py` | `fetch_article` guardrails: 27 malicious URLs, DNS tricks with a fake resolver, redirect to loopback, TLS hostname check |
| `python tests/failure_check.py` | Retry vs stop for every failure class (simulated Tavily), real bogus-key call, claim-level provenance checks |
| `python tests/agent_check.py` | The agent loop with a scripted fake model: budgets, partial reports, terminal errors, fallback model, prompt injection, memory |
| `python tests/memory_check.py` | Key matching, new / still / dropped, and a save → load → second run → reset cycle (backend) |
| `python tests/keys_check.py` | Your Gemini and Tavily keys work (1 credit) |

## Reports and traces

- `reports/run1.md`, `reports/run2.md`: the graded runs, at least a day apart.
- `traces/run1.jsonl`, `traces/run2.jsonl`: one JSON object per line, written as it happens. Every model call
  and tool call has `step`, `kind`, `tool`, `args`, `status`, `latency_ms`, and `tokens` (model calls) or
  `credits` (searches); retries, budget stops, and memory load/save are logged too.

## Troubleshooting

- **The tracker says the backend can't be reached.** Start it (step 1). The tracker checks the backend, the
  keys, and the login before its first model call, so nothing is spent when one of them is missing.
- **`Gemini server error (503 UNAVAILABLE): high demand`.** Google's free tier is busy. Each call is retried
  6 times with backoff, then the run switches once to the fallback model in `config.yaml`. Try again later if
  both are overloaded.
- **`Gemini daily quota used up`.** The free tier allows a limited number of requests per day per model;
  the run stops instead of retrying. The quota resets at midnight Pacific time.
- **`No module named 'httpx'` (or `bs4`, `yaml`).** The venv that's active doesn't have the tracker packages:
  activate `backend\.venv` and run `python -m pip install -r tracker\requirements.txt`.
- **Vite says port 5173 is in use.** This is deliberate (`strictPort`): the backend's CORS allowlist names
  port 5173 exactly. Free the port, or add your origin to `CORS_ORIGINS` in `backend/.env` and restart the backend.
- **Frontend shows "Cannot reach the server".** The backend isn't running on port 8000, or the page's origin
  isn't in `CORS_ORIGINS`. Use `http://localhost:5173` or `http://127.0.0.1:5173`.
- **First request after a while is slow.** Neon's free tier suspends idle databases; the first query wakes it (~1 s).
- **`npm ci` or `npm run dev` fails with a syntax error.** Check `node --version` (needs 20.19+ or 22.12+).

## Project structure

```
config.yaml                tracker policy: topic, K, model, instructions, tools, limits, fetch rules
AGENT.md                   design questions, answered from the traces
reports/  traces/          run1 / run2 reports and trace logs
tracker/
  __main__.py              CLI: run, status, reset, save
  agent.py                 the agent loop: budgets, untrusted-text fences, finish handling
  llm/base.py, gemini.py   provider-neutral types, pacer; Gemini REST adapter + error classification
  tools.py                 search_web / fetch_article / finish, callable from the command line
  search.py                Tavily search + error classification
  fetch.py, urlguard.py    fetch with SSRF guardrails, IP pinning, redirect re-checks, size/time caps
  extract.py, urls.py      HTML -> text; URL canonicalization
  report.py                claim-level provenance checks
  memory.py, backend.py    new / still / dropped, key matching; client for the A1 tracker API
  errors.py, budget.py     transient vs terminal failures + retry; run budgets
  trace.py, render.py      JSONL trace; Markdown report
backend/
  app/
    main.py                FastAPI app, CORS, startup table creation, /healthz
    models.py              users + tracker tables
    schemas.py             A1 request/response shapes (UserOut has no password field)
    tracker_schemas.py     tracker request/response shapes (sizes capped, URLs checked)
    security.py, deps.py   argon2id, JWT; get_current_user -> 401
    routers/auth.py        register, login, me
    routers/users.py       GET/PATCH/DELETE /api/users/:id (ownership -> 404)
    routers/tracker.py     /api/tracker/* (every query filtered by the token's user)
  seed.py                  creates the NYUgrader account (idempotent)
frontend/
  src/lib/api.js           fetch wrapper: bearer token, errors, auto-logout on 401
  src/lib/safeUrl.js       only http(s) URLs become links
  src/pages/               Login, Register, Home, Account, Tracker
tests/                     see Tests
```
