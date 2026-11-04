# Assignment 2 — decision and conflict log

Written while implementing Assignment 2 (agentic orchestration). The instructor's PDF
(`FNMS Fall 2026 Assignments 2.pdf`) is the source of truth; `command.md` / `claude.md` (identical files,
written with ChatGPT) operationalize it. Each entry says what was decided and why, so every part can be
defended at the demo.

## Baseline (before any change), 2026-11-04

| Check | Result |
|---|---|
| `tests/guard_check.py` | 34 passed, **1 failed**: "redirect to loopback is rejected at the redirect hop" — the test depends on a live public redirect service, which answered `HTTP 403`. Not a code regression; network-dependent. |
| `tests/agent_check.py` | 47 passed, 0 failed |
| `tests/failure_check.py` | 29 passed, 0 failed |
| `tests/memory_check.py` | 11 passed, 0 failed |
| Neon database | reachable; 3 runs in `tracker_runs`; 6 tables |
| A1B single-agent baseline (from committed traces) | run1: 16 model calls, 7 searches, 6 fetches, 165,064 tokens, 68.4 s. run2: 16 / 7 / 2 fetches, 181,553 tokens, 68.6 s |

Uncommitted work found on branch `run-button` (`tracker/__main__.py`, `tracker/backend.py`: a `TRACKER_TOKEN`
login path) was left untouched and is not part of the A2 commits. A2 work happens on branch
`a2-orchestration`.

## Conflicts between command.md and the PDF (PDF wins)

| # | command.md says | PDF says | Resolution |
|---|---|---|---|
| C1 | §0.3: ask before costly live runs, deployment, external pushes | (nothing) | The student explicitly said "full access, don't ask me to approve anything". Live free-tier runs were made without asking. Actions the safety rules forbid me to do (typing API keys into the Render dashboard, creating accounts) are left to the student and listed in README "Render deployment". |
| C2 | §4.1 "show the **last successful** run's top K as stale" | §6 "shows **last run's** top K, marked stale" | If the previous run was itself a researcher failure it has no top K of its own, so "last run's top K" would be empty. Implemented: the most recent run that has a ranked top K. In the common case (previous run succeeded) both readings agree. |
| C3 | §5.5 a supported excerpt must "substantiate the claim" (checked by code) | §9 code checks only that the excerpt (≤300 chars) appears in the fetched text | Substantiation is a semantic judgment; code cannot decide it. Code enforces what the PDF requires (length, presence after normalization) plus a minimum excerpt length (documented in ORCHESTRATION.md as an extra rule, not a normalization). The model is asked to pick an excerpt that substantiates the claim. |
| C4 | §9 test matrix: "researcher rejects unsafe fetch too" for `http://169.254.169.254/` | p.7 test table lists the metadata-URL test as a claim test | The researcher's payload has no URLs (`{topic, k}`), so the test cannot be sent to it as a request. The researcher reuses `tracker/urlguard.py`, whose existing guard tests (`tests/guard_check.py`) cover it; the verifier test sends the URL in a claim. |
| C5 | §3 "Successful result body: `{"developments": [...]}`" plus suggested `status`/`usage`/`error` | p.3–4: table column is "result out"; p.4 "On partial or failed, `error` says why"; 500 → `status: failed, retryable: false` | The response envelope is `{request_id, run_id, status, result, usage, error}`; `result` holds exactly the PDF's "result out" object. The field names `status`, `error.code/message/retryable` come from the PDF; `usage` and the `result` wrapper are my choice (the PDF asks workers to report usage but doesn't name the field). |
| C6 | §4.3 keep `max_searches` | p.3 envelope budget has exactly `max_steps`, `max_fetches`, `max_tokens` | The wire budget keeps exactly the PDF's three keys. Search calls are capped by the researcher's own policy (`researcher.max_searches` in `config.yaml`), and reported in `usage.searches` for cost accounting. |
| C7 | §1 repository is `fnms-assignment-1b` | — | The local A2 checkout's `origin` is still the 1B repository. Nothing was pushed to it; see README for how to push to an A2 repository for Render. |

## Design decisions

| # | Decision | Why |
|---|---|---|
| D1 | Orchestrator is **plain code** inside the existing FastAPI backend (`backend/app/orchestrator/`). No LLM routing. | The flow is fixed (research → verify → report). Nothing in it needs judgment; code is cheaper, testable, and can't be prompt-injected. |
| D2 | `shared/` holds the wire contract (Pydantic models, error codes, canonical JSON hash) and two stateless helpers (JSONL trace writer, worker HTTP plumbing: auth, 400 mapping, idempotency registry class). No service imports another service's package. | PDF §2 forbids importing *another service's code to call it*; it allows a shared schema file. The helpers hold no state shared between processes: each process creates its own registry and trace file. Deviates from the earlier note "shared holds schemas only" to avoid three copies of the same 400/401/409/503 logic, which would be harder to keep consistent. |
| D3 | The verifier gets its **own copy** of the fetch guardrails (`verifier/fetchguard.py`, from `tracker/urlguard.py` + `tracker/fetch.py` + `tracker/extract.py`), not an import of `tracker/`. | `tracker/` is the researcher's code. The verifier deploys alone to Render and must not depend on it (agreed earlier with the student). |
| D4 | Every redirect hop is one fetch. `fetch_article` gained an `on_hop` callback that is called **before** each outgoing request and may refuse it (budget). The researcher's worker budget counts hops, not `fetch_article` calls. | PDF §3: "A fetch is one HTTP request to a web page, redirects included." |
| D5 | One Gemini key for both workers, called **sequentially** (researcher, then verifier); each worker paces its own model calls. | Free tier; running both at once would contend for the same per-minute quota. |
| D6 | Verifier: one fetch per **distinct URL** per request (cached within that request only), and one model call per distinct URL that judges all claims citing it. | Cuts fetches and model calls when several claims cite the same article. The cache lives only for one request, so nothing leaks between runs. |
| D7 | Verifier model output is JSON (Gemini `responseMimeType: application/json`); code validates every field, ignores claim ids it didn't ask about, downgrades `supported` when the excerpt check fails, and never upgrades. | PDF §9 and §11. |
| D8 | The researcher worker runs with **no memory** (no DB, no seen-URL list). The orchestrator does key dedupe against known developments and new/still/dropped classification (logic re-implemented in `backend/app/orchestrator/memory.py`). | PDF: only the orchestrator reads or writes state. The A1B tracker's memory came from the database. |
| D9 | No `ALTER TABLE` on existing tables. New table `tracker_claims` (created by `create_all`) stores each claim, its URL, its verdict and the verifier's evidence. Which worker failed and the stale-fallback run id are stored in the run's existing `stats` JSON and exposed as explicit API fields. | `create_all` creates new tables but never alters existing ones; avoiding a migration on the shared Neon database removes the riskiest step. |
| D10 | Researcher evidence quotes are **not** on the wire (contract has `claim_id, text, source_url` only). `tracker_sources.evidence` is left `NULL` for A2 runs; the only evidence shown is the verifier's, from `tracker_claims`. | Keeps the researcher's and the verifier's evidence from being confused (command.md §6). |
| D11 | Runs are started from the web page (`POST /api/tracker/orchestrate`, user JWT) or the CLI (`python -m orchestrator_cli`), run in a background thread, and use the orchestrator `run_id` as the `tracker_runs.id`. | One id ties the DB row to all three trace logs. |
| D12 | At the start of each run the orchestrator sends `GET /healthz` to the verifier in the background, while the researcher works. | Wakes a sleeping Render instance during the ~1–2 minutes the researcher needs, so the cold start mostly overlaps useful work. The real `/verify` timeout still allows for a full cold start in case the wake-up fails. |
| D13 | Idempotency registry is in memory (per process), keyed by `request_id`, value = SHA-256 of the canonical JSON body (sorted keys, no whitespace) + state (in-flight / done) + stored status code and body. Only final answers (200, 500) are stored; 503s are not. Bounded: oldest finished entries evicted past 1,000 entries. | PDF §7. A Render restart loses it (discussed in ORCHESTRATION.md Q3). |
| D14 | Worker response envelope: `{request_id, run_id, status, result, usage, error}` (see C5). `usage = {steps, fetches, tokens, searches}`; `searches` is 0 for the verifier. | Same shape for both workers; orchestrator validates it identically. |
| D15 | Orchestrator charging: valid response → the usage it reports (must be ≤ the share it was given, else invalid); no answer / timeout / malformed / 5xx → the **full share**; 400/401 → 0 (the worker rejects these before doing any work). Each `request_id` charged once regardless of attempts. | PDF §8. |
| D16 | Traces: each service appends to its own file, `traces/<service>.jsonl` (verifier on Render: also stdout, plus an authenticated `GET /trace?run_id=` to download its log). Required fields always present; extra fields allowed. Inbound requests are logged once when answered, with server-side handling time; caller-side latency is logged by the caller on its own monotonic clock. | PDF §13. |
