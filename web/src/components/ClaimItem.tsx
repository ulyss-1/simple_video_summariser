import type { components } from '../api'
import { UNATTRIBUTED } from './AnalysisView'
import { Timestamp } from './Timestamp'

type Claim = components['schemas']['ClaimOut']
type Quote = components['schemas']['QuoteOut']
type Topic = components['schemas']['TopicOut']

// The same wording and markup as the single-run view (AnalysisView, #50), as
// items a table cell can hold. Everything is a React text child: nothing here
// builds HTML or a URL, and every time goes through <Timestamp>.

function speakerLabel(speaker: string): string {
  return speaker === 'unknown' ? UNATTRIBUTED : speaker
}

export function ClaimItem({ claim }: { claim: Claim }) {
  return (
    <li>
      <span>{claim.text}</span> <span>Speaker: {speakerLabel(claim.speaker)}</span>
      {claim.confidence !== null && <span> Confidence: {claim.confidence}</span>}{' '}
      <Timestamp seconds={claim.start_sec} />
    </li>
  )
}

export function QuoteItem({ quote }: { quote: Quote }) {
  return (
    <li>
      <q>{quote.text}</q> <span>Speaker: {speakerLabel(quote.speaker)}</span> <Timestamp seconds={quote.start_sec} />
    </li>
  )
}

export function TopicItem({ topic }: { topic: Topic }) {
  return (
    <li>
      <strong>{topic.title}</strong> <Timestamp seconds={topic.start_sec} />
      {topic.summary ? <p>{topic.summary}</p> : null}
    </li>
  )
}
