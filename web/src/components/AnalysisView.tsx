import type { components } from '../api'
import { parseRoster } from '../lib/roster'
import { Timestamp } from './Timestamp'

type Analysis = components['schemas']['AnalysisOut']

/** Same wording as the server-side renderer (services/api/render.py). */
export const UNATTRIBUTED = 'Unattributed'

function speakerLabel(speaker: string): string {
  return speaker === 'unknown' ? UNATTRIBUTED : speaker
}

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
              <li key={i}>
                <strong>{t.title}</strong> <Timestamp seconds={t.start_sec} />
                {t.summary ? <p>{t.summary}</p> : null}
              </li>
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
              <li key={i}>
                <span>{c.text}</span> <span>Speaker: {speakerLabel(c.speaker)}</span>
                {c.confidence !== null && <span> Confidence: {c.confidence}</span>}{' '}
                <Timestamp seconds={c.start_sec} />
              </li>
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
              <li key={i}>
                <q>{q.text}</q> <span>Speaker: {speakerLabel(q.speaker)}</span>{' '}
                <Timestamp seconds={q.start_sec} />
              </li>
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
