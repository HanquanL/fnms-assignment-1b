# Home Base — FNMS Assignment 1

A personal "home base": a full-stack web app with account registration, login, and account management,
built as the foundation for future assignments. FastAPI + Postgres (Neon) backend, React + Vite frontend.

## Tech stack

| Layer    | Choice |
|----------|--------|
| Database | PostgreSQL on [Neon](https://neon.com) (hosted, nothing to install) |
| Backend  | Python 3.11, FastAPI, SQLAlchemy 2, psycopg 3 |
| Auth     | Hand-written: argon2id password hashing (`argon2-cffi`), HS256 JWT bearer tokens (`PyJWT`) |
| Frontend | React 19, Vite, Tailwind CSS v4, React Router, lucide-react |

## Prerequisites

- **Python 3.11+**
- **Node.js 22.12+** (or 20.19+) — required by Vite
- **Git**
- No local database needed: the app uses a hosted Neon Postgres database whose connection string is in `backend/.env`.

## Quick start

Open **two terminals** in the repository root. Start the backend first.

### 1. Backend — terminal 1

**Windows (PowerShell)**
```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python seed.py
uvicorn app.main:app --port 8000
```

**macOS / Linux**
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python seed.py
uvicorn app.main:app --port 8000
```

> If PowerShell refuses to run `Activate.ps1`, skip activation and call the venv's Python directly:
> `.venv\Scripts\python.exe -m pip install -r requirements.txt`, then `.venv\Scripts\python.exe seed.py`,
> then `.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`.

Check it's up: <http://localhost:8000/healthz> → `{"status":"ok"}`. Interactive API docs: <http://localhost:8000/docs>.

### 2. Frontend — terminal 2

```bash
cd frontend
npm ci
npm run dev
```

Open **<http://localhost:5173>**.

### 3. Sign in

| Username    | Password       |
|-------------|----------------|
| `NYUgrader` | `Courant2026!` |

The account already exists in the database. `python seed.py` (step 1) recreates it if it was deleted, and
`python seed.py --reset` restores its password if it was changed. Both are safe to run any number of times.

## Environment

`backend/.env` and `frontend/.env` are **committed on purpose** so the project runs as-is.
They contain only a throwaway Neon database created for this assignment and a random JWT secret used
nowhere else. `.env.example` files list the same variables with placeholders.

**backend/.env**

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Postgres connection string (SQLAlchemy format: `postgresql+psycopg://...`) |
| `JWT_SECRET` | HMAC key used to sign tokens |
| `JWT_ALGORITHM` | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime (60) |
| `CORS_ORIGINS` | Comma-separated frontend origins allowed to call the API |

**frontend/.env**

| Variable | Purpose |
|----------|---------|
| `VITE_API_URL` | Backend base URL (`http://localhost:8000`). Anything prefixed `VITE_` ships to the browser, so no secrets here. |

## Database & migrations

No migration command is needed. On startup the backend runs SQLAlchemy `create_all`, which creates the
`users` table if it doesn't exist and never modifies or drops existing data. Data lives in Neon, so it
survives backend restarts.

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

A user is always returned as `{id, username, email, created_at, updated_at}`.

**Errors:** 401 missing/invalid/expired token or bad login · 404 not your account (see below) ·
409 username/email taken · 400 wrong current password · 422 invalid input.

**The three rules**
1. **No password hash ever leaves the API.** Every route serializes users through one response model
   (`UserOut`) that has no password field.
2. **No token, bad token, or expired token → 401.** One dependency (`get_current_user`) handles every case.
3. **You can't touch another user's account → 404.** GET, PATCH, and DELETE on an `:id` that isn't yours all
   return the same `404 User not found` as an id that doesn't exist (or isn't a valid UUID).
   *Why 404 and not 403:* a 403 confirms that the account exists, which lets an attacker enumerate valid ids.
   With 404, "someone else's account" and "no such account" are indistinguishable. (GitHub does the same
   for private repositories.) Auth is still checked first, so a missing token is 401, not 404.

## Tests

With the backend running, from the repository root:

```powershell
backend\.venv\Scripts\python.exe tests\api_check.py      # Windows
```
```bash
backend/.venv/bin/python tests/api_check.py              # macOS / Linux
```

It registers two throwaway accounts, exercises every endpoint and all three rules (68 checks), then deletes them.

## Troubleshooting

- **Vite says port 5173 is in use.** This is deliberate (`strictPort`): the backend's CORS allowlist names
  port 5173 exactly. Free the port, or add your origin to `CORS_ORIGINS` in `backend/.env` and restart the backend.
- **Frontend shows "Cannot reach the server".** The backend isn't running on port 8000, or the page's origin
  isn't in `CORS_ORIGINS`. Use `http://localhost:5173` or `http://127.0.0.1:5173`.
- **First request after a while is slow.** Neon's free tier suspends idle databases; the first query wakes it (~1 s).
- **`npm ci` or `npm run dev` fails with a syntax error.** Check `node --version` (needs 20.19+ or 22.12+).

## Project structure

```
backend/
  app/
    main.py          FastAPI app, CORS, startup table creation, /healthz
    config.py        Settings loaded from backend/.env
    db.py            Engine + session
    models.py        User table
    schemas.py       Request/response shapes (UserOut has no password field)
    security.py      argon2id hashing, JWT create/verify
    deps.py          get_current_user -> 401
    routers/auth.py  register, login, me
    routers/users.py GET/PATCH/DELETE /api/users/:id (ownership -> 404)
  seed.py            Creates the NYUgrader account (idempotent)
frontend/
  src/lib/api.js           fetch wrapper: bearer token, errors, auto-logout on 401
  src/auth/AuthContext.jsx current user + login/register/logout/update/delete
  src/auth/guards.jsx      route guards (UX only; the API enforces access)
  src/pages/               Login, Register, Home, Account
tests/api_check.py         End-to-end API checks
```