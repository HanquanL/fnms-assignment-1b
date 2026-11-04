import { useCallback, useEffect, useRef, useState } from 'react'
import { Archive, ExternalLink, FileText, History, Loader2, Newspaper, Play, Radar } from 'lucide-react'
import { trackerApi } from '../lib/api'
import { hostOf, safeHref } from '../lib/safeUrl'
import { Alert, Card } from '../components/ui.jsx'

// Everything this page shows that came from the web (titles, summaries,
// evidence, URLs, reasons) is rendered as React text, which escapes it. There is
// no dangerouslySetInnerHTML and no Markdown-to-HTML here on purpose: a page the
// tracker read can contain <script>, and it must show up as those characters.

const STATUS_STYLES = {
  complete: 'bg-emerald-500/10 text-emerald-300 ring-emerald-500/20',
  partial: 'bg-amber-500/10 text-amber-300 ring-amber-500/20',
  failed: 'bg-red-500/10 text-red-300 ring-red-500/20',
  fetched: 'bg-emerald-500/10 text-emerald-300 ring-emerald-500/20',
  skipped: 'bg-slate-500/10 text-slate-300 ring-slate-500/20',
  rejected: 'bg-red-500/10 text-red-300 ring-red-500/20',
  new: 'bg-indigo-500/15 text-indigo-200 ring-indigo-400/30',
  still: 'bg-slate-500/10 text-slate-300 ring-slate-500/20',
  dropped: 'bg-amber-500/10 text-amber-300 ring-amber-500/20',
  // Assignment 2 claim labels
  supported: 'bg-emerald-500/10 text-emerald-300 ring-emerald-500/20',
  unsupported: 'bg-red-500/10 text-red-300 ring-red-500/20',
  unreachable: 'bg-orange-500/10 text-orange-300 ring-orange-500/20',
  unverified: 'bg-slate-500/10 text-slate-300 ring-slate-500/30',
  stale: 'bg-amber-500/15 text-amber-200 ring-amber-400/30',
  running: 'bg-indigo-500/15 text-indigo-200 ring-indigo-400/30',
}
const VERDICT_HELP = {
  supported: 'the verifier fetched the cited page itself and found this excerpt in it',
  unsupported: 'the verifier read the cited page and it does not state this claim',
  unreachable: 'the verifier could not (or was not allowed to) fetch the cited page',
  unverified: 'no valid verdict: the verifier failed, timed out, or did not reach this claim',
}
const ARTICLE_HELP = {
  fetched: 'downloaded and read',
  skipped: 'already read in an earlier run',
  rejected: 'refused by a guardrail before any request',
  failed: 'the request was made but did not work',
}

function Badge({ value, title }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
        STATUS_STYLES[value] ?? STATUS_STYLES.still
      }`}
    >
      {value}
    </span>
  )
}

function when(iso) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

// A link only for http(s) URLs. A URL a guardrail rejected (localhost, 169.254.169.254, ...)
// is never a link either: clicking it would send *your* browser where the tracker refused to go.
function WebLink({ url, label, clickable = true }) {
  const href = clickable ? safeHref(url) : null
  if (!href) return <span className="break-all text-slate-400">{label ?? url}</span>
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      className="inline-flex items-start gap-1 break-all text-indigo-300 hover:text-indigo-200 hover:underline"
    >
      {label ?? url}
      <ExternalLink className="mt-1 size-3 shrink-0" />
    </a>
  )
}

function SectionTitle({ icon: Icon, children, right }) {
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
      <h2 className="flex items-center gap-2 font-medium">
        <Icon className="size-4 text-slate-400" />
        {children}
      </h2>
      {right}
    </div>
  )
}

// One claim with its verifier label. Evidence is shown only for supported claims; an
// unreachable URL is not a link (the verifier may have refused it as unsafe).
function Claim({ claim }) {
  return (
    <li className="text-sm">
      <div className="flex flex-wrap items-start gap-2">
        <Badge value={claim.verdict} title={VERDICT_HELP[claim.verdict]} />
        <span className="min-w-0 flex-1 text-slate-200">{claim.text}</span>
      </div>
      <div className="mt-1 pl-1 text-xs">
        <WebLink url={claim.source_url} label={hostOf(claim.source_url)} clickable={claim.verdict !== 'unreachable'} />
      </div>
      {claim.verdict === 'supported' && claim.evidence && (
        <blockquote className="mt-1 border-l-2 border-emerald-500/30 pl-3 text-xs italic leading-relaxed text-slate-400">
          {claim.evidence}
        </blockquote>
      )}
    </li>
  )
}

function Development({ item, stale = false }) {
  const dropped = item.change === 'dropped'
  const claims = item.claims ?? []
  return (
    <article className={`rounded-xl p-4 ring-1 ring-white/10 ${dropped ? 'bg-white/[0.02]' : 'bg-white/5'}`}>
      <div className="flex items-start gap-3">
        <span
          className={`grid size-8 shrink-0 place-items-center rounded-lg text-sm font-semibold ${
            dropped ? 'bg-white/5 text-slate-500' : 'bg-indigo-500/15 text-indigo-200'
          }`}
        >
          {item.rank ?? '–'}
        </span>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className={`font-medium ${dropped ? 'text-slate-400' : 'text-slate-100'}`}>{item.title}</h3>
            <Badge value={stale ? 'stale' : item.change} />
          </div>
          <p className="font-mono text-xs text-slate-500">{item.key}</p>
          <p className={`text-sm leading-relaxed ${dropped ? 'text-slate-500' : 'text-slate-300'}`}>{item.summary}</p>
          {!dropped && claims.length > 0 && (
            <ul className="space-y-3 pt-1">
              {claims.map((c) => (
                <Claim key={c.claim_id} claim={c} />
              ))}
            </ul>
          )}
          {!dropped && claims.length === 0 && item.sources.length > 0 && (
            <ul className="space-y-2 pt-1">
              {item.sources.map((s) => (
                <li key={s.url} className="text-sm">
                  <WebLink url={s.url} label={s.title || hostOf(s.url)} />
                  {s.evidence &&
                    s.evidence.split(' ... ').map((quote) => (
                      <blockquote
                        key={quote}
                        className="mt-1 border-l-2 border-white/10 pl-3 text-xs italic leading-relaxed text-slate-400"
                      >
                        {quote}
                      </blockquote>
                    ))}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </article>
  )
}

// Researcher failed: no fresh results. Last run's top K is shown, clearly separated and marked stale.
function StaleTopK({ stale }) {
  return (
    <section className="space-y-3 rounded-xl bg-amber-500/5 p-4 ring-1 ring-amber-500/20">
      <h3 className="flex items-center gap-2 text-sm font-medium text-amber-200">
        <Archive className="size-4" />
        No new results. Last run's top {stale.rankings.length} (stale, from {when(stale.finished_at)})
      </h3>
      {stale.rankings.length === 0 ? (
        <p className="text-sm text-slate-400">There is no earlier top K to show.</p>
      ) : (
        <div className="space-y-3">
          {stale.rankings.map((item) => (
            <Development key={item.key} item={item} stale />
          ))}
        </div>
      )}
    </section>
  )
}

function Report({ run, hasPrevious }) {
  const ranked = run.rankings.filter((r) => r.change !== 'dropped')
  if (run.stale_top_k) return <StaleTopK stale={run.stale_top_k} />
  const groups = hasPrevious
    ? [
        ['New since last run', ranked.filter((r) => r.change === 'new')],
        ['Still in top K', ranked.filter((r) => r.change === 'still')],
        ['Dropped', run.rankings.filter((r) => r.change === 'dropped')],
      ]
    : [[`Top ${run.k}`, ranked]]

  return (
    <div className="space-y-6">
      {run.rankings.length === 0 && (
        <p className="text-sm text-slate-400">This run produced no verified developments.</p>
      )}
      {groups.map(([title, items]) => (
        <section key={title}>
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-500">
            {title} <span className="text-slate-600">({items.length})</span>
          </h3>
          {items.length === 0 ? (
            <p className="text-sm text-slate-500">None.</p>
          ) : (
            <div className="space-y-3">
              {items.map((item) => (
                <Development key={item.key} item={item} />
              ))}
            </div>
          )}
        </section>
      ))}
    </div>
  )
}

function RunMeta({ run }) {
  const s = run.stats || {}
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-slate-400">
      <Badge value={run.status} />
      <span>{when(run.started_at)}</span>
      <span className="font-mono text-xs">{run.model}</span>
      {s.steps !== undefined && (
        <span>
          {s.steps} model calls · {s.searches} searches · {s.fetches} fetches · {Number(s.tokens_total).toLocaleString()}{' '}
          tokens · {s.duration_s}s
        </span>
      )}
      {s.kind === 'a2' && (
        <span className="w-full text-xs text-slate-500">
          researcher: <span className="text-slate-300">{s.researcher_status}</span> · verifier:{' '}
          <span className="text-slate-300">{s.verifier_status}</span>
          {s.searches !== undefined && <> · {s.searches} searches</>}
          {' '}· budget {s.charged_steps}/{s.budget_steps} calls, {s.charged_fetches}/{s.budget_fetches} fetches
        </span>
      )}
      <WorkerProblems run={run} />
      {run.stop_reason && <span className="w-full text-amber-300/80">{run.stop_reason}</span>}
    </div>
  )
}

function WorkerProblems({ run }) {
  const failed = run.failed_workers ?? []
  const partial = run.partial_workers ?? []
  if (failed.length === 0 && partial.length === 0) return null
  return (
    <span className="flex flex-wrap gap-2">
      {failed.map((w) => (
        <span key={`fw-${w}`} className="inline-flex items-center gap-1 text-xs text-red-300">
          <Badge value="failed" title={`${w} failed`} /> {w}
        </span>
      ))}
      {partial.map((w) => (
        <span key={`pw-${w}`} className="inline-flex items-center gap-1 text-xs text-amber-300">
          <Badge value="partial" /> {w}
        </span>
      ))}
    </span>
  )
}

function ClaimCounts({ counts }) {
  const order = ['supported', 'unsupported', 'unreachable', 'unverified']
  if (!counts || Object.keys(counts).length === 0) return null
  return (
    <span className="text-xs text-slate-500">
      {order
        .filter((v) => counts[v])
        .map((v) => `${counts[v]} ${v}`)
        .join(' · ')}
    </span>
  )
}

function RunHistory({ runs, selectedId, onSelect }) {
  return (
    <ul className="divide-y divide-white/5">
      {runs.map((r) => (
        <li key={r.id}>
          <button
            type="button"
            onClick={() => onSelect(r.id)}
            aria-current={r.id === selectedId}
            className={`flex w-full flex-wrap items-center gap-x-4 gap-y-1 rounded-lg px-3 py-2.5 text-left text-sm transition ${
              r.id === selectedId ? 'bg-white/10' : 'hover:bg-white/5'
            }`}
          >
            <span className="min-w-36 text-slate-200">{when(r.started_at)}</span>
            <Badge value={r.status} />
            <WorkerProblems run={r} />
            <ClaimCounts counts={r.claim_counts} />
            <span className="text-slate-400">
              <span className="text-indigo-300">+{r.counts.new} new</span> · {r.counts.still} still ·{' '}
              {r.counts.dropped} dropped
            </span>
            <span className="text-xs text-slate-500">
              {r.counts.fetched} fetched · {r.counts.skipped} skipped · {r.counts.rejected} rejected · {r.counts.failed}{' '}
              failed
            </span>
          </button>
        </li>
      ))}
    </ul>
  )
}

function Articles({ articles }) {
  if (articles.length === 0) return <p className="text-sm text-slate-400">No articles in this run.</p>
  return (
    <ul className="divide-y divide-white/5">
      {articles.map((a, i) => (
        <li key={`${a.canonical_url}-${i}`} className="flex flex-col gap-1 py-3 text-sm sm:flex-row sm:gap-4">
          <div className="sm:w-24 sm:shrink-0">
            <Badge value={a.status} title={ARTICLE_HELP[a.status]} />
          </div>
          <div className="min-w-0 flex-1 space-y-1">
            {a.title && <p className="text-slate-200">{a.title}</p>}
            <WebLink url={a.url} clickable={a.status !== 'rejected'} />
            {a.reason && (
              <p className="text-xs text-slate-500">
                {a.http_status && !a.reason.startsWith('HTTP') ? `HTTP ${a.http_status} · ` : ''}
                {a.reason}
              </p>
            )}
          </div>
          <span className="text-xs text-slate-500 sm:shrink-0">{when(a.fetched_at)}</span>
        </li>
      ))}
    </ul>
  )
}

function EmptyState() {
  return (
    <Card className="text-center">
      <Radar className="mx-auto size-8 text-slate-500" />
      <h2 className="mt-3 font-medium">No tracker runs yet</h2>
      <p className="mx-auto mt-2 max-w-md text-sm text-slate-400">
        Run the tracker from the repository root and its report shows up here:
      </p>
      <pre className="mx-auto mt-3 w-fit rounded-lg bg-black/40 px-4 py-2 text-left text-xs text-slate-300">
        python -m tracker run --label run1
      </pre>
    </Card>
  )
}

// Start an orchestrated run (researcher + verifier) and poll until it is saved.
function RunButton({ onFinished }) {
  const [active, setActive] = useState(null)
  const [err, setErr] = useState('')
  const timer = useRef(null)

  useEffect(() => () => clearTimeout(timer.current), [])

  function poll(id) {
    timer.current = setTimeout(() => {
      trackerApi
        .orchestration(id)
        .then((p) => {
          setActive(p)
          if (p.state === 'running') return poll(id)
          if (p.state === 'error') setErr(p.error || 'The run failed before it could be saved.')
          setActive(null)
          onFinished(id)
        })
        .catch((e) => {
          setErr(e.message)
          setActive(null)
        })
    }, 3000)
  }

  function start() {
    setErr('')
    trackerApi
      .orchestrate()
      .then((p) => {
        setActive(p)
        poll(p.run_id)
      })
      .catch((e) => setErr(e.message))
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={start}
        disabled={active !== null}
        className="inline-flex items-center gap-2 rounded-lg bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-60"
      >
        {active ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
        {active ? `Running: ${active.stage}` : 'Run tracker (researcher + verifier)'}
      </button>
      {err && <span className="max-w-sm text-right text-xs text-red-300">{err}</span>}
    </div>
  )
}

export default function TrackerPage() {
  const [runs, setRuns] = useState([])
  const [detail, setDetail] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback((preferId) => {
    return Promise.all([
      trackerApi.runs(),
      preferId
        ? trackerApi.run(preferId).catch(() => null)
        : trackerApi.latest().catch((e) => (e.status === 404 ? null : Promise.reject(e))),
    ]).then(([list, chosen]) => {
      setRuns(list)
      setDetail(chosen)
      setSelectedId(chosen?.id ?? null)
    })
  }, [])

  useEffect(() => {
    let cancelled = false
    load()
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [load])

  function selectRun(id) {
    if (id === selectedId) return
    setSelectedId(id)
    setDetailLoading(true)
    setError('')
    trackerApi
      .run(id)
      .then(setDetail)
      .catch((e) => setError(e.message))
      .finally(() => setDetailLoading(false))
  }

  if (loading) {
    return (
      <div className="grid place-items-center py-24">
        <Loader2 className="size-6 animate-spin text-slate-500" />
      </div>
    )
  }

  const index = detail ? runs.findIndex((r) => r.id === detail.id) : -1
  const hasPrevious = index >= 0 && runs.slice(index + 1).some((r) => r.status !== 'failed')
  const isLatest = detail && runs.find((r) => r.status !== 'failed')?.id === detail.id

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-sm text-slate-400">Tracker</p>
          <h1 className="text-3xl font-semibold tracking-tight">{detail?.topic ?? 'Open-weight AI model releases'}</h1>
          <p className="mt-1 text-slate-400">
            The top {detail?.k ?? 5} developments. Each claim is checked by an independent verifier that fetches
            the cited page itself.
          </p>
        </div>
        <RunButton onFinished={(id) => load(id).catch((e) => setError(e.message))} />
      </header>

      <Alert>{error}</Alert>

      {runs.length === 0 ? (
        <EmptyState />
      ) : (
        <>
          {detail && (
            <Card>
              <SectionTitle
                icon={FileText}
                right={detailLoading && <Loader2 className="size-4 animate-spin text-slate-500" />}
              >
                {isLatest ? 'Latest report' : 'Report'}
              </SectionTitle>
              <div className="mb-6">
                <RunMeta run={detail} />
              </div>
              <Report run={detail} hasPrevious={hasPrevious} />
              {detail.report_md && (
                <details className="mt-6 text-sm">
                  <summary className="cursor-pointer text-slate-400 hover:text-slate-200">
                    Show the report as Markdown (plain text)
                  </summary>
                  <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-black/40 p-4 text-xs text-slate-300">
                    {detail.report_md}
                  </pre>
                </details>
              )}
            </Card>
          )}

          <Card>
            <SectionTitle icon={History}>Run history</SectionTitle>
            <RunHistory runs={runs} selectedId={selectedId} onSelect={selectRun} />
          </Card>

          {detail && (
            <Card>
              <SectionTitle icon={Newspaper} right={<span className="text-xs text-slate-500">{when(detail.started_at)}</span>}>
                Articles this run
              </SectionTitle>
              <Articles articles={detail.articles} />
            </Card>
          )}
        </>
      )}
    </div>
  )
}
