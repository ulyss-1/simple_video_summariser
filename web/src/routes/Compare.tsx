import { Fragment, useEffect, useId, type ReactNode } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router'
import { AnalysisView } from '../components/AnalysisView'
import { ClaimItem, QuoteItem, TopicItem } from '../components/ClaimItem'
import { Player } from '../components/Player'
import { AnalysesApiError, useAnalyses, type AnalysisRun } from '../hooks/useAnalyses'
import { useVideo } from '../hooks/useVideo'
import {
  alignByWindow,
  countClaims,
  formatCost,
  formatDuration,
  formatUtcMinutes,
  formatUtcSeconds,
  parseAnalysisParam,
  resolveSelection,
  selectionMatchesUrl,
} from '../lib/compare'
import { parseRoster } from '../lib/roster'
import { parseVideoId } from '../lib/videoId'

const NOT_FOUND_NOTICE = 'A requested analysis was not found for this video. Showing the default comparison.'

/** Marks the replaced history entry, so the notice outlives the URL rewrite. */
interface CompareState {
  unknownRequested: true
}

const label = (r: AnalysisRun) => `${r.model} · ${r.prompt_version}`

export function Compare() {
  const videoId = parseVideoId(useParams().videoId)
  if (videoId === null) return <NotFound />
  // Keyed by id so nothing from a previous video survives a navigation.
  return <ComparePage key={videoId} videoId={videoId} />
}

function NotFound() {
  useEffect(() => {
    document.title = 'Video not found'
  }, [])
  return (
    <section>
      <h1>Video not found</h1>
      <p>
        <Link to="/">Back to Library</Link>
      </p>
    </section>
  )
}

function ComparePage({ videoId }: { videoId: string }) {
  const analyses = useAnalyses(videoId)
  const video = useVideo(videoId)
  const heading = video.data?.title || videoId

  useEffect(() => {
    document.title = `Compare: ${heading}`
  }, [heading])

  if (analyses.data === undefined) {
    if (analyses.isError) {
      if (analyses.error instanceof AnalysesApiError && analyses.error.status === 404) return <NotFound />
      return (
        <section>
          <h1>{heading}</h1>
          <p role="alert">Could not load the analyses</p>
          <button
            type="button"
            onClick={() => {
              void analyses.refetch()
            }}
          >
            Retry
          </button>
        </section>
      )
    }
    return (
      <section>
        <h1>{heading}</h1>
        <p role="status">Loading analyses…</p>
      </section>
    )
  }

  const { runs, total, capped } = analyses.data
  return (
    <article>
      <h1>{heading}</h1>
      {runs.length === 0 ? (
        <>
          <p>This video has no analyses yet.</p>
          <p>
            <Link to={`/videos/${videoId}`}>Video details</Link>
          </p>
        </>
      ) : (
        <>
          <p>
            <Link to={`/videos/${videoId}`}>Video details</Link>
          </p>
          <Player videoId={videoId} title={heading === videoId ? undefined : heading} />
          {capped && <p>{`Showing the newest ${runs.length} of ${total} analyses`}</p>}
          <Comparison runs={runs} />
        </>
      )}
    </article>
  )
}

function Comparison({ runs }: { runs: AnalysisRun[] }) {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const location = useLocation()
  const selection = resolveSelection(runs, params.getAll('a'), params.getAll('b'))
  const { a: idA, b: idB } = selection
  const matches = selectionMatchesUrl(params, idA, idB)

  const withSelection = (a: number | null, b: number | null) => {
    const next = new URLSearchParams(params)
    next.delete('a')
    next.delete('b')
    if (a !== null) next.set('a', String(a))
    if (b !== null) next.set('b', String(b))
    return next
  }

  // The URL always holds the actual selection, so copying it reproduces the
  // comparison. A rewrite replaces the entry: it is not a user's choice.
  const rewrite = !matches
  const unknownNow = selection.unknownRequested
  useEffect(() => {
    if (!rewrite) return
    const state: CompareState | null = unknownNow ? { unknownRequested: true } : null
    void navigate({ search: `?${withSelection(idA, idB).toString()}` }, { replace: true, state })
  })

  const choose = (a: number | null, b: number | null) => {
    setParams(withSelection(a, b))
  }
  const pick = (side: 'a' | 'b', raw: string) => {
    const id = parseAnalysisParam([raw])
    if (id === null || !runs.some((r) => r.id === id)) return
    if (side === 'a') choose(id, idB)
    else choose(idA, id)
  }

  const showNotice = unknownNow || (location.state as Partial<CompareState> | null)?.unknownRequested === true
  const runA = runs.find((r) => r.id === idA)
  const runB = runs.find((r) => r.id === idB)
  if (runA === undefined) return null

  if (runB === undefined) {
    return (
      <>
        {showNotice && <p>{NOT_FOUND_NOTICE}</p>}
        <p>Only one analysis exists for this video, so there is nothing to compare yet.</p>
        <Picker name="Analysis A" runs={runs} value={runA.id} blocked={null} onChange={(raw) => pick('a', raw)} />
        <AnalysisView analysis={runA} />
      </>
    )
  }

  return (
    <>
      {showNotice && <p>{NOT_FOUND_NOTICE}</p>}
      <Notices a={runA} b={runB} />
      <div>
        <Picker name="Analysis A" runs={runs} value={runA.id} blocked={runB.id} onChange={(raw) => pick('a', raw)} />{' '}
        <Picker name="Analysis B" runs={runs} value={runB.id} blocked={runA.id} onChange={(raw) => pick('b', raw)} />{' '}
        <button
          type="button"
          onClick={() => {
            choose(runB.id, runA.id)
          }}
        >
          Swap A and B
        </button>
      </div>
      <Details a={runA} b={runB} />
      <SideBySide title="TL;DR" a={runA} b={runB}>
        {(run) => <p style={{ whiteSpace: 'pre-line' }}>{run.tldr}</p>}
      </SideBySide>
      <SideBySide title="Speakers" a={runA} b={runB}>
        {(run) => {
          const roster = parseRoster(run.speaker_roster)
          if (roster.length === 0) return <p>No roster</p>
          return (
            <ul>
              {roster.map((s, i) => (
                <li key={i}>
                  {s.name}
                  {s.role !== null && ` (${s.role})`}
                </li>
              ))}
            </ul>
          )
        }}
      </SideBySide>
      <SideBySide title="Topics" a={runA} b={runB}>
        {(run) =>
          run.topics.length === 0 ? (
            <p>No topics extracted.</p>
          ) : (
            <ol>
              {[...run.topics]
                .sort((x, y) => x.seq - y.seq)
                .map((t, i) => (
                  <TopicItem key={i} topic={t} />
                ))}
            </ol>
          )
        }
      </SideBySide>
      <Aligned title="Claims" noun="claims" a={runA} b={runB} itemsA={runA.claims} itemsB={runB.claims}>
        {(c) => <ClaimItem claim={c} />}
      </Aligned>
      <Aligned title="Quotes" noun="quotes" a={runA} b={runB} itemsA={runA.quotes} itemsB={runB.quotes}>
        {(q) => <QuoteItem quote={q} />}
      </Aligned>
    </>
  )
}

function Picker({
  name,
  runs,
  value,
  blocked,
  onChange,
}: {
  name: string
  runs: AnalysisRun[]
  value: number
  blocked: number | null
  onChange: (raw: string) => void
}) {
  const id = useId()
  return (
    <>
      <label htmlFor={id}>{name}</label>{' '}
      <select
        id={id}
        value={String(value)}
        onChange={(e) => {
          onChange(e.target.value)
        }}
      >
        {runs.map((r) => (
          <option key={r.id} value={String(r.id)} disabled={r.id === blocked}>
            {`${r.model} · ${r.prompt_version} · ${formatUtcMinutes(r.created_at)} · ${r.transcript_source} · #${r.id}`}
          </option>
        ))}
      </select>
    </>
  )
}

function Notices({ a, b }: { a: AnalysisRun; b: AnalysisRun }) {
  return (
    <>
      {a.transcript_id !== b.transcript_id && (
        <p>
          {`These runs used different transcripts (${a.transcript_source} vs ${b.transcript_source}), so differences may come from the transcript rather than the model or prompt.`}
        </p>
      )}
      {a.chunk_strategy !== b.chunk_strategy && (
        <p>
          {`These runs used different chunk strategies (${a.chunk_strategy} vs ${b.chunk_strategy}), so differences may come from how the transcript was split.`}
        </p>
      )}
      {a.model === b.model && a.prompt_version === b.prompt_version && (
        <p>Same model and prompt version: differences show run-to-run variation.</p>
      )}
    </>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = useId()
  return (
    <section aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      {children}
    </section>
  )
}

function Heads({ a, b, first }: { a: AnalysisRun; b: AnalysisRun; first?: string }) {
  return (
    <thead>
      <tr>
        {first !== undefined && <th scope="col">{first}</th>}
        <th scope="col">{`Analysis A (${label(a)})`}</th>
        <th scope="col">{`Analysis B (${label(b)})`}</th>
      </tr>
    </thead>
  )
}

/** One row, A's cell before B's. Not aligned: each side is its own list. */
function SideBySide({
  title,
  a,
  b,
  children,
}: {
  title: string
  a: AnalysisRun
  b: AnalysisRun
  children: (run: AnalysisRun) => ReactNode
}) {
  return (
    <Section title={title}>
      <table>
        <Heads a={a} b={b} />
        <tbody>
          <tr>
            <td>{children(a)}</td>
            <td>{children(b)}</td>
          </tr>
        </tbody>
      </table>
    </Section>
  )
}

function Aligned<T extends { start_sec: number | null }>({
  title,
  noun,
  a,
  b,
  itemsA,
  itemsB,
  children,
}: {
  title: string
  noun: string
  a: AnalysisRun
  b: AnalysisRun
  itemsA: T[]
  itemsB: T[]
  children: (item: T) => ReactNode
}) {
  const rows = alignByWindow(itemsA, itemsB)
  const cell = (items: T[]) =>
    items.length === 0 ? (
      `No ${noun} in this window`
    ) : (
      <ul>
        {items.map((item, i) => (
          <Fragment key={i}>{children(item)}</Fragment>
        ))}
      </ul>
    )
  return (
    <Section title={title}>
      {rows.length === 0 ? (
        <p>{`No ${noun} extracted in either analysis.`}</p>
      ) : (
        <table>
          <Heads a={a} b={b} first="Time window" />
          <tbody>
            {rows.map((row) => (
              <tr key={row.window ?? 'none'}>
                <th scope="row">{row.label}</th>
                <td>{cell(row.a)}</td>
                <td>{cell(row.b)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  )
}

function Details({ a, b }: { a: AnalysisRun; b: AnalysisRun }) {
  const ca = countClaims(a.claims)
  const cb = countClaims(b.claims)
  const n = (x: number) => String(x)
  const rows: [string, string, string][] = [
    ['model', a.model, b.model],
    ['prompt_version', a.prompt_version, b.prompt_version],
    ['chunk_strategy', a.chunk_strategy, b.chunk_strategy],
    ['transcript_source', a.transcript_source, b.transcript_source],
    ['created_at', formatUtcSeconds(a.created_at), formatUtcSeconds(b.created_at)],
    ['input_tokens', n(a.input_tokens), n(b.input_tokens)],
    ['output_tokens', n(a.output_tokens), n(b.output_tokens)],
    ['cost_usd', formatCost(a.cost_usd), formatCost(b.cost_usd)],
    ['duration_ms', formatDuration(a.duration_ms), formatDuration(b.duration_ms)],
    ['claim count', n(a.claims.length), n(b.claims.length)],
    ['quote count', n(a.quotes.length), n(b.quotes.length)],
    ['topic count', n(a.topics.length), n(b.topics.length)],
    ['claims with speaker unknown', n(ca.unknownSpeaker), n(cb.unknownSpeaker)],
    ['claims, confidence high', n(ca.high), n(cb.high)],
    ['claims, confidence medium', n(ca.medium), n(cb.medium)],
    ['claims, confidence low', n(ca.low), n(cb.low)],
    ['claims, confidence not given', n(ca.notGiven), n(cb.notGiven)],
  ]
  return (
    <Section title="Run details">
      <table>
        <Heads a={a} b={b} first="Field" />
        <tbody>
          {rows.map(([field, va, vb]) => (
            <tr key={field}>
              <th scope="row">
                {field}
                {va !== vb && <> (differs)</>}
              </th>
              <td>{va}</td>
              <td>{vb}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  )
}
