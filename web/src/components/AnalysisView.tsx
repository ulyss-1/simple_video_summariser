import type { components } from '../api'
import { parseRoster } from '../lib/roster'
import { ClaimItem, QuoteItem, TopicItem } from './ClaimItem'

type Analysis = components['schemas']['AnalysisOut']

function utcIso(value: string): string {
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toISOString()
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  const id = `section-${title.replace(/\W+/g, '').toLowerCase()}`
  return (
    <section aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      {children}
    </section>
  )
}

/** Everything is a React text child: nothing here builds HTML or a URL. */
export function AnalysisView({ analysis }: { analysis: Analysis }) {
  const roster = parseRoster(analysis.speaker_roster)
  return (
    <>
      <Section title="TL;DR">
        <p style={{ whiteSpace: 'pre-line' }}>{analysis.tldr}</p>
      </Section>

      {roster.length > 0 && (
        <Section title="Speakers">
          <ul>
            {roster.map((s, i) => (
              <li key={i}>
                {s.name}
                {s.role !== null && ` (${s.role})`}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Topics">
        {analysis.topics.length === 0 ? (
          <p>No topics extracted.</p>
        ) : (
          <ol>
            {analysis.topics.map((t, i) => (
              <TopicItem key={i} topic={t} />
            ))}
          </ol>
        )}
      </Section>

      <Section title="Claims">
        {analysis.claims.length === 0 ? (
          <p>No claims extracted.</p>
        ) : (
          <ul>
            {analysis.claims.map((c, i) => (
              <ClaimItem key={i} claim={c} />
            ))}
          </ul>
        )}
      </Section>

      <Section title="Quotes">
        {analysis.quotes.length === 0 ? (
          <p>No quotes extracted.</p>
        ) : (
          <ul>
            {analysis.quotes.map((q, i) => (
              <QuoteItem key={i} quote={q} />
            ))}
          </ul>
        )}
      </Section>

      <footer>
        <p>
          Model: {analysis.model} · Prompt version: {analysis.prompt_version} · Analysed:{' '}
          <time dateTime={utcIso(analysis.created_at)}>{utcIso(analysis.created_at)}</time>
        </p>
      </footer>
    </>
  )
}
