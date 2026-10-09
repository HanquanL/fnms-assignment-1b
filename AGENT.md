# AGENT.md

The tracker finds the top 5 open-weight AI model releases, ranks and summarizes them with sources, and reports
what changed since the last run. Numbers below come from `traces/run1.jsonl` and `traces/run2.jsonl`
unless a development run (`traces/dev/`, not committed) is named. Run 1: 2026-10-08 19:57 UTC.
Run 2: 2026-10-09 20:31 UTC, 24.5 hours later.

## 1. Workflow vs. agent

**The model decides** what to search for, which results are worth fetching, which events count as
"developments", how to rank them, how to word each claim, and which sentence of an article supports it.
Those are judgment calls about text, which is what the model is for.

**The code decides** everything that has to be reliable, bounded, or safe:

| Decision | Where |
|----------|-------|
| When the run stops: model calls, searches, fetches, tokens, wall time | `tracker/budget.py` |
| How fast to call the model (≤ 14 requests/minute, before any 429) | `Pacer` in `tracker/llm/base.py` |
| Whether a failure is retried, how long to wait, when to give up | `tracker/errors.py`, `classify_gemini`, `classify_tavily` |
| Whether a URL may be fetched at all | `tracker/urlguard.py` |
| Whether a URL was already read in an earlier run (skip it, free) | `do_fetch` in `tracker/agent.py` |
| Whether a site that refused us (401/403/429) is tried again this run | `do_fetch` |
| Whether the final report is accepted | `tracker/report.py` |
| Whether a short report is enough or the model should keep looking | `do_finish` |
| Whether a development is new, still in the top K, or dropped; whether two keys are the same development | `tracker/memory.py` |

**One decision moved out of the model: whether a claim is supported by its source.** The first version
asked the model, in the instructions, to cite only articles it had fetched and to copy evidence word for
word. The traces showed what that is worth. In development run `20261008-182436`, the model's report cited
search-result URLs it never fetched, and once a URL it simply made up:
`https://thenextweb.com/news/not-fetched-but-let-use-techcrunch-url`. In development run `20261008-185659`
the summary said Mistral Large 4 has "1.05 trillion" parameters while the cited Wired article says
"1 trillion". "1.05" isn't in any article that run fetched; it most likely came from a search snippet
(MarkTechPost's headline for the same story says "1.05T", and that site refuses our fetches). An instruction
can't stop that; only a check can.

So `finish(report)` is now a proposal. Each development is 1–4 claims, each claim carries a URL and a quoted
sentence, and `report.py` accepts a claim only if (1) the URL is an article this run fetched, or a source
verified in an earlier run, (2) the quoted sentence appears in that article's text (case, whitespace and
curly quotes normalized), and (3) every number in the claim appears somewhere in that article. Replaying the
Mistral summary through it gives `the claim says 1.05 but https://www.wired.com/... never does`. Rejections go
back to the model with the closest real sentence from the article and the list of URLs it may cite; after 3
rejections the code keeps only the claims that verify. In run 1 the first `finish` was rejected, the second
passed with 3 developments (the code then asked once for more, since budget remained), and the third was
accepted. Why move it: the 2-point provenance check is exactly the failure the model makes most, and
it's cheap and deterministic to check in code.

What the check does not catch: a claim with no numbers that says more than its source (e.g. calling a model
"multimodal" when the article doesn't). Each claim is shown next to its quote in the report and the app so a
reader can see the gap; a second model call per claim could judge it, at the cost of free-tier quota.

## 2. The network

**Run 1 made 32 HTTP round trips** (plus a DNS lookup and a TCP+TLS handshake for most of them):

| Service | Requests | Time | Notes |
|---------|---------:|-----:|-------|
| Gemini `generativelanguage.googleapis.com` | 16 | 52.8 s | 1 per model call; 0 retries |
| Tavily `api.tavily.com` | 7 | 12.6 s | 1 per search; 1.1–3.7 s each |
| Article sites (6 hosts) | 6 | 2.9 s | 3 fetched (mistral.ai, alphaxiv.org, lindy.ai); 2 × 403 (marktechpost.com, cybernews.com); 1 × 429 (venturebeat.com) |
| A1 backend `localhost:8000` | 3 | — | login, `GET /api/tracker/state`, `POST /api/tracker/runs` (backend → Neon behind it) |

**Where the time went (71.4 s):** 74% in model calls (52.8 s, which includes waiting on our own pacer),
18% in search, 4% in fetches. The model calls started 4.0–4.8 s apart, almost exactly the pacer's
60/14 = 4.29 s, and run 1 sent 14 requests in its busiest minute, the most the pacer allows. So **the run was
paced by our own rate limiter** rather than by Gemini, which is also why it hit no 429s. Run 1's trace counts
the pacer's waits inside each call's `latency_ms`, so for run 2 the trace logs `pacer_wait_ms` separately,
and the backend calls get `latency_ms` too. Run 2 confirms it:

| Run 2 (68.6 s run + backend calls) | Requests | Time |
|------------------------------------|---------:|-----:|
| Waiting on our own pacer | — | 28.4 s |
| Gemini answering | 16 | 26.5 s (0.8–4.2 s per call) |
| Tavily | 7 | 12.3 s |
| Article sites | 2 | 1.4 s (3 more were skipped as already read: no request) |
| A1 backend: login + load state / save run | 2 / 1 | 3.0 s / 3.2 s |

**28 round trips**, and the single largest share of the run is the pacer: we spent more time deliberately
waiting (to stay under 15 requests/minute) than Gemini spent answering. The backend calls are slow for a
local server because each one goes on to Neon, whose free tier suspends an idle database and wakes it on the
first query. Skipping the 3 known articles saved 3 requests and 3 of the 15 fetches in the budget.

**Tokens:** 165,064, of which 161,817 (98%) were input. Each call resends the whole conversation, so the
prompt grew from 1,118 tokens on call 1 to 18,773 on call 16; output was only 3,247 tokens. Article text is
the main cost: each fetch adds up to 8,000 characters that stay in every later prompt.

**Things the trace showed that are worth changing:** every Gemini call opens a new connection
(`httpx.Client` per call), so each pays a TCP+TLS handshake; a shared client would reuse one connection.
Prompt caching would make the repeated prefix cheaper on a paid tier.

## 3. "New"

**Identity is the development's key, `org/model/event`** (e.g. `qwen/qwen-4/release`), not the URL.
A development and the articles about it are separate tables, so a new article about a known development
becomes one more source on the existing row.

1. **Same URL** — URLs are canonicalized (lowercase host, default port, tracking parameters such as `utm_*`
   and `fbclid` removed, trailing slash, sorted query). A URL fetched in any earlier run is skipped before any
   request and recorded as `skipped`.
2. **New URL, known development** — the model sees every known development (key, title, a source and its
   evidence) and is told to reuse the key. Because it doesn't always copy keys exactly, `memory.identity()`
   normalizes them: org aliases (`alibaba` → `qwen`, `deepseek-ai` → `deepseek`), punctuation removed, the
   org prefix stripped from the model name, and the event part ignored. So
   `deepseek-ai/deepseek-v4-1-flash/launch` maps onto the known `deepseek/v4.1-flash/release`.
3. **new / still / dropped** — `still` if the key was in the last run's top K, `new` if not, and `dropped`
   if it was in the last top K and isn't now. A run that stopped early (partial) drops nothing: it didn't
   look hard enough to say something fell out.

**What run 2 did with run 1's memory** (3 seen URLs, 3 developments, last top 3):

- The model tried to fetch all three of run 1's sources again (`alphaxiv.org/.../introducing-beam`,
  `mistral.ai/news/mistral-large-4`, `lindy.ai/blog/deepseek-v4-flash`). Each was answered `skipped` without a
  request, and the model was pointed to the known evidence instead.
- Mistral Large 4, Reflection Beam, and DeepSeek V4.1 Flash came back under their exact run-1 keys, citing the
  run-1 sources and evidence, so they are **Still in top K**.
- EmbeddingGemma 2 (blog.google) and Cloudflare Clef-omni (blog.cloudflare.com) are **New since last run**.
- **Dropped** is empty, and here that's correct twice over: all three of run 1's developments are still in the
  list, and run 2 ended `partial` (below), which drops nothing by design.
- Run 2 didn't happen to meet a new URL for a known development: the model reused run 1's sources rather
  than finding new coverage of them, so `renamed_keys` is empty in the trace. That path is exercised in
  `tests/memory_check.py` against the real backend: an article about Qwen 4 at a new URL, keyed
  `alibaba/qwen4/weights`, is matched to the known `qwen/qwen-4/release`, which ends up with one row and two
  sources.

Why run 2 is `partial`: its 5 developments all verified, but one claim about EmbeddingGemma 2 quoted a
sentence that isn't in the article, three times in a row, so the code dropped that claim and kept the rest
(`finish rejected 3 times; kept 5 developments whose evidence verified`). We count that as partial on purpose:
the report says something the model wanted to say was cut.

**A case the method gets wrong:** two different events about the same model collapse into one development,
because identity ignores the event part. If Qwen 4 is released on Monday and its license changes on
Thursday, the license change shows up as "still in top K" with a second source, not as something new. The
opposite error also exists: an org the alias table doesn't know (say `thudm/glm-5` vs. `z-ai/glm-5`) stays
two developments.

## 4. Failure: a 429 comes back

Gemini uses 429 `RESOURCE_EXHAUSTED` for both "too many requests this minute" and "no more requests today".
The only difference is in the error details, so `classify_gemini` reads them
(`tracker/llm/gemini.py`, lines 40–47):

```python
    if code == 429:
        daily = [q for q in quota_ids if "PerDay" in q]
        if daily:
            return TerminalError(
                f"Gemini daily quota used up ({daily[0]}). It resets at midnight Pacific time; "
                f"retrying today won't help.")
        which = next((q for q in quota_ids if "PerMinute" in q), "rate limit")
        return TransientError(f"Gemini per-minute limit (429, {which})", retry_after=_retry_delay(details))
```

`quota_ids` come from the `QuotaFailure` detail's `violations[].quotaId`
(e.g. `GenerateRequestsPerDayPerProjectPerModel-FreeTier`), and `_retry_delay` reads the `RetryInfo` detail
(e.g. `"28s"`). The single retry loop is `call_with_retry` (`tracker/errors.py`, lines 64–81):

```python
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except TransientError as e:
            if attempt == policy.max_attempts:
                raise RetriesExhausted(f"{what}: still failing after {attempt} attempts ({e})") from e
            if e.retry_after is not None and e.retry_after > policy.max_delay:
                raise TerminalError(
                    f"{what}: server asked us to wait {e.retry_after:.0f}s, longer than the "
                    f"{policy.max_delay:.0f}s we allow; treating it as a quota, not a blip ({e})"
                ) from e
            delay = backoff_delay(policy, attempt, e.retry_after)
            if on_retry:
                on_retry(attempt, e, delay)
            sleep(delay)
```

**Per-minute limit:** a `TransientError`. The loop waits `max(1s, 2s, 4s, 8s, 16s + jitter, the server's
RetryInfo)` and tries again, up to 6 attempts (`config.yaml: retry`); each retry is a `retry` line in the
trace. If it still fails, `RetriesExhausted` ends the run as partial, and if the model was answering 503s
the run first switches once to the fallback model. In practice the pacer keeps us under 15 requests/minute,
so this path is rare: run 1 had none, while the development runs on 2026-10-08 hit many 503 "high demand"
errors, most of which succeeded on the first or second retry.

**Daily cap:** a `TerminalError` on the first response: no sleep, no second request. The run stops, writes a
partial report from what it already verified, and says when the quota resets. Retrying it would only burn
time until midnight Pacific. As a second line of defense, any 429 that asks us to wait longer than 60 s is
treated as a quota rather than a blip.

Tavily is classified the same way (`classify_tavily` in `tracker/search.py`): 429 → transient with
`Retry-After`; 401/403 bad key, 432 monthly plan limit, 433 pay-as-you-go limit → terminal. A bogus key is
tested for both services (`tests/failure_check.py`, `tests/agent_check.py`); note Gemini answers a bad key
with **400** `INVALID_ARGUMENT` / `API_KEY_INVALID`, not 401, which we found by sending one.

## 5. Budget

**One run costs $0.** Both services are on free tiers with no billing enabled, so the spend cap is zero:
running out returns an error, never a bill. In quota:

| Resource | Run 1 | Run 2 | Run limit (`config.yaml`) | Free tier |
|----------|------:|------:|---------:|-----------|
| Gemini requests (`gemini-3.5-flash-lite`) | 16 | 16 | 25 | 15 / minute, 500 / day |
| Gemini tokens | 165,064 | 181,553 | 500,000 | 250,000 / minute |
| … in the busiest minute | 161,254 | 176,673 | — | 250,000 |
| Tavily credits (basic search) | 7 | 7 | 8 | 1,000 / month |
| Article fetches (requests) | 6 | 2 (+3 skipped) | 15 | free (3 of run 1's 6 sites refused us) |

**Running daily, which free tier runs out first?** Gemini's daily quota never does: 16 of 500 requests a day,
reset every night (even a run that uses all 25 model calls is 5%). **Tavily is the first and only one that
could:** 7 credits a run is 210 a month, 21% of the 1,000 monthly credits, so with its monthly reset it never
runs out either. Without the reset it would run out on **day 143** (142 runs × 7 = 994 credits, and the 143rd
run needs more than the 6 left), or on day 126 if every run used all 8 searches.

The limit that actually binds is a different one: **Gemini's 250,000 tokens per minute.** Run 1 used 161,254
tokens in its busiest minute (65%) and run 2 used 176,673 (71%), after only 16 calls each, because every call
resends the growing conversation (run 2's prompt grew from 1,603 to 20,613 tokens). A run that uses all 25 calls would pass the per-minute token limit in its last minute and spend
it in per-minute 429 retries. Cheaper fixes than a paid tier: send less article text per fetch, or drop old
article text from the conversation once its evidence is recorded.
