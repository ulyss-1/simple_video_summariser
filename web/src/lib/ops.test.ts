import { describe, expect, it } from 'vitest'
import type { operations } from '../api'
import {
  bulkIncludesTranscribe,
  bulkRetryBody,
  bulkRetryErrorMessage,
  bulkRetryMessage,
  bulkScopeLabel,
  buildJobsRequest,
  cancelMessage,
  cancelPath,
  canCancel,
  isCancelling,
  stateBadgeClass,
  stateLabel,
  isBulkRetryOut,
  collapseError,
  depthTable,
  ERROR_CLASSES,
  errorClassInfo,
  formatUtc,
  isHealth,
  isJobList,
  JOB_KINDS,
  JOB_STATES,
  NO_CLASS,
  opsSearch,
  parseOpsParams,
  retryMessage,
  retryPath,
} from './ops'

type Query = NonNullable<operations['get_ops_jobs_ops_jobs_get']['parameters']['query']>
type Equal<A, B> = (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false
// Compile-time: the sets match the generated enums exactly (a backend change breaks `tsc`).
const kindsMatch: Equal<(typeof JOB_KINDS)[number], NonNullable<Query['kind']>> = true
const statesMatch: Equal<(typeof JOB_STATES)[number], NonNullable<Query['state']>> = true
const classesMatch: Equal<
  (typeof ERROR_CLASSES)[number] | typeof NO_CLASS,
  NonNullable<Query['error_class']>
> = true

const p = (s: string) => new URLSearchParams(s)

describe('shared sets', () => {
  it('match the generated enums, in the documented order', () => {
    expect([kindsMatch, statesMatch, classesMatch]).toEqual([true, true, true])
    expect([...JOB_KINDS]).toEqual(['ingest', 'transcribe', 'analyze'])
    expect([...JOB_STATES]).toEqual(['pending', 'running', 'done', 'dead'])
    expect(ERROR_CLASSES).toHaveLength(9)
    expect(NO_CLASS).toBe('none')
  })
})

describe('parseOpsParams', () => {
  it('defaults to dead with nothing else and no correction', () => {
    const r = parseOpsParams(p(''))
    expect(r).toEqual({ filters: { state: 'dead', kind: null, errorClass: null }, before: null, corrected: null })
  })

  it.each([...JOB_STATES])('accepts state=%s', (s) => {
    const r = parseOpsParams(p(`state=${s}`))
    expect(r.filters.state).toBe(s)
    expect(r.corrected).toBeNull()
  })
  it.each([...JOB_KINDS])('accepts kind=%s', (k) => {
    expect(parseOpsParams(p(`kind=${k}`)).filters.kind).toBe(k)
  })
  it.each([...ERROR_CLASSES, 'none'])('accepts error_class=%s', (c) => {
    expect(parseOpsParams(p(`error_class=${c}`)).filters.errorClass).toBe(c)
  })

  it.each(['DEAD', 'Dead', '', ' dead', 'dead ', 'dead,done', 'x', 'constructor', '__proto__', 'all'])(
    'drops state=%j and falls back to dead',
    (v) => {
      const r = parseOpsParams(new URLSearchParams([['state', v]]))
      expect(r.filters.state).toBe('dead')
      expect(r.corrected?.has('state')).toBe(false)
    },
  )
  it.each(['Ingest', 'INGEST', '', 'notify', 'ingest ', 'toString'])('drops kind=%j', (v) => {
    const r = parseOpsParams(new URLSearchParams([['kind', v]]))
    expect(r.filters.kind).toBeNull()
    expect(r.corrected?.has('kind')).toBe(false)
  })
  it.each(['bug', 'Bug', '', 'NONE', 'None', 'OTHER', 'hasOwnProperty'])('drops error_class=%j', (v) => {
    const r = parseOpsParams(new URLSearchParams([['error_class', v]]))
    expect(r.filters.errorClass).toBeNull()
    expect(r.corrected?.has('error_class')).toBe(false)
  })

  it.each(['1', '7', '100', '9007199254740991'])('accepts before=%s', (v) => {
    const r = parseOpsParams(p(`before=${v}`))
    expect(r.before).toBe(Number(v))
    expect(r.corrected).toBeNull()
  })
  it.each([
    '0', '-1', '1.5', 'abc', '1e3', '9007199254740992', '', '01', '+1', ' 1', '1 ', '0x10',
    '99999999999999999999', '١٢٣', '1\n', '12abc', 'abc12',
  ])('drops before=%j', (v) => {
    const r = parseOpsParams(new URLSearchParams([['before', v]]))
    expect(r.before).toBeNull()
    expect(r.corrected?.has('before')).toBe(false)
  })

  it('drops a repeated parameter instead of picking one', () => {
    const r = parseOpsParams(p('state=pending&state=done&kind=ingest&kind=analyze&before=5&before=6'))
    expect(r.filters).toEqual({ state: 'dead', kind: null, errorClass: null })
    expect(r.before).toBeNull()
    expect(r.corrected?.toString()).toBe('')
  })

  it('keeps valid and unknown parameters when it corrects another', () => {
    const r = parseOpsParams(p('state=pending&kind=BAD&x=1'))
    expect(r.filters.state).toBe('pending')
    expect(r.corrected?.toString()).toBe('state=pending&x=1')
  })
})

describe('opsSearch and buildJobsRequest', () => {
  const none = { state: 'dead', kind: null, errorClass: null } as const
  it('writes only non-default parameters to the view URL', () => {
    expect(opsSearch(none, null)).toBe('')
    expect(opsSearch({ state: 'pending', kind: 'ingest', errorClass: 'BUG' }, 12)).toBe(
      'state=pending&kind=ingest&error_class=BUG&before=12',
    )
    expect(opsSearch({ ...none, errorClass: 'none' }, null)).toBe('error_class=none')
  })
  it('always sends state and limit=50 and omits unset parameters', () => {
    expect(buildJobsRequest(none, null).toString()).toBe('state=dead&limit=50')
    expect(buildJobsRequest({ state: 'done', kind: 'analyze', errorClass: 'none' }, 99).toString()).toBe(
      'state=done&kind=analyze&error_class=none&before_id=99&limit=50',
    )
  })
})

describe('errorClassInfo', () => {
  it.each([
    ['PERMANENT_SOURCE', 'Video removed, private or blocked. Retry only if it is back'],
    ['TRANSIENT_NETWORK', 'Network error'],
    ['RATE_LIMITED', 'Rate-limited or bot-checked by YouTube'],
    ['TOOL_FAILURE', 'yt-dlp is probably broken. Update yt-dlp, then retry'],
    ['LLM_INVALID_OUTPUT', 'The model returned invalid output'],
    ['LLM_UNAVAILABLE', 'The LLM provider was unreachable'],
    ['RESOURCE', 'Disk full or out of memory. Fix that before retrying'],
    ['BUG', 'Unhandled exception. See the traceback'],
    ['CANCELLED', 'Cancelled on request; retry to resume'],
  ])('%s has its hint', (code, hint) => {
    expect(errorClassInfo(code)).toEqual({ label: code, hint })
  })
  it('null is "No class" with the lost-worker hint', () => {
    expect(errorClassInfo(null)).toEqual({ label: 'No class', hint: 'Often a lost worker (stale heartbeat)' })
  })
  it.each(['SOMETHING_NEW', '', 'constructor', '__proto__', 'toString', '<b>x</b>'])(
    'unknown %j is shown as-is with no hint',
    (v) => {
      expect(errorClassInfo(v)).toEqual({ label: v, hint: null })
    },
  )
})

describe('formatUtc', () => {
  it('formats in UTC whatever the process time zone is', () => {
    expect(formatUtc('2026-10-01T23:30:00-07:00')).toBe('2026-10-02 06:30 UTC')
    expect(formatUtc('2026-01-05T00:05:09Z')).toBe('2026-01-05 00:05 UTC')
    expect(formatUtc('2026-12-31T23:59:59.999+00:00')).toBe('2026-12-31 23:59 UTC')
  })
  it('shows a dash for null and keeps an unparseable value as text', () => {
    expect(formatUtc(null)).toBe('—')
    expect(formatUtc('not a date')).toBe('not a date')
  })
})

describe('collapseError', () => {
  it('null, empty and blank are "no error"', () => {
    for (const v of [null, '', '   ', '\n\n']) expect(collapseError(v)).toBeNull()
  })
  it('keeps a short single line as is', () => {
    expect(collapseError('boom')).toBe('boom')
  })
  it('keeps only the first line of a multi-line traceback', () => {
    expect(collapseError('Traceback (most recent call last):\n  File "x"\nValueError: bad')).toBe(
      'Traceback (most recent call last):',
    )
    expect(collapseError('first\r\nsecond')).toBe('first')
    expect(collapseError('first\rsecond')).toBe('first')
  })
  it('skips leading blank lines', () => {
    expect(collapseError('\n\n  real\nmore')).toBe('  real')
  })
  it('cuts at 200 characters including the ellipsis', () => {
    expect(collapseError('a'.repeat(200))).toBe('a'.repeat(200))
    const cut = collapseError('a'.repeat(201)) as string
    expect(cut).toBe('a'.repeat(199) + '…')
    expect(cut).toHaveLength(200)
    expect(collapseError('x'.repeat(8192)) as string).toHaveLength(200)
  })
  it('never splits a surrogate pair', () => {
    const cut = collapseError('😀'.repeat(300)) as string
    expect([...cut]).toHaveLength(200)
    expect(cut.endsWith('…')).toBe(true)
    expect(cut).toBe('😀'.repeat(199) + '…')
  })
})

describe('retryPath', () => {
  it('builds the path for a positive safe integer', () => {
    expect(retryPath(1)).toBe('ops/jobs/1/retry')
    expect(retryPath(Number.MAX_SAFE_INTEGER)).toBe(`ops/jobs/${Number.MAX_SAFE_INTEGER}/retry`)
  })
  it.each([0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, 1e21])('refuses %s', (id) => {
    expect(() => retryPath(id)).toThrow()
  })
})

describe('retryMessage', () => {
  const http = (status: number, body: unknown = undefined) => ({ type: 'http', status, body }) as const
  it('maps 200 using the requested id', () => {
    expect(retryMessage(42, http(200, { id: 999, state: 'dead' }))).toBe('Queued again: job #42 is pending')
    expect(retryMessage(42, http(200))).toBe('Queued again: job #42 is pending')
  })
  it('404', () => expect(retryMessage(1, http(404))).toBe('This job no longer exists'))
  it('429', () => expect(retryMessage(1, http(429))).toBe('Too many requests. Try again shortly.'))
  it('503', () => expect(retryMessage(1, http(503))).toBe('The database is unavailable. Try again shortly.'))
  it.each([...JOB_STATES])('409 job is not dead, state %s', (s) => {
    expect(retryMessage(1, http(409, { detail: 'job is not dead', state: s }))).toBe(`Job is already ${s}`)
  })
  it.each([
    [{ detail: 'job is not dead', state: 'exploded' }],
    [{ detail: 'job is not dead', state: '<img src=x>' }],
    [{ detail: 'job is not dead', state: 5 }],
    [{ detail: 'job is not dead', state: null }],
    [{ detail: 'job is not dead' }],
    [{ detail: 'job is not dead', state: 'constructor' }],
  ])('409 job is not dead with unusable state %j', (body) => {
    expect(retryMessage(1, http(409, body))).toBe('Job is no longer dead')
  })
  it('409 superseded uses the newer id when it is a positive safe integer', () => {
    expect(retryMessage(1, http(409, { detail: 'superseded by a newer job', job_id: 77 }))).toBe(
      'A newer job #77 exists for this video',
    )
  })
  it.each([0, -3, 1.5, '77', null, Number.MAX_SAFE_INTEGER + 1, '<b>1</b>', undefined])(
    '409 superseded with unusable job_id %j',
    (job_id) => {
      expect(retryMessage(1, http(409, { detail: 'superseded by a newer job', job_id }))).toBe(
        'A newer job exists for this video',
      )
    },
  )
  it.each([
    [{ detail: 'something else' }],
    [{ detail: 'Job is not dead' }],
    [{ detail: 5 }],
    [{}],
    ['job is not dead'],
    [null],
    [undefined],
    [[1]],
  ])('any other 409 body %j', (body) => {
    expect(retryMessage(1, http(409, body))).toBe('The job could not be retried')
  })
  it.each([400, 401, 403, 415, 422, 500, 502, 504, 201, 204, 302])('other status %i', (status) => {
    expect(retryMessage(1, http(status, { detail: 'secret' }))).toBe('Retry failed')
  })
  it('a network failure', () => {
    expect(retryMessage(1, { type: 'network' })).toBe('Retry failed')
  })
})

describe('depthTable', () => {
  it('always has the known kinds and states, zero-filled', () => {
    const t = depthTable({})
    expect(t.kinds).toEqual(['ingest', 'transcribe', 'analyze'])
    expect(t.states).toEqual(['pending', 'running', 'done', 'dead'])
    for (const k of t.kinds) for (const s of t.states) expect(t.count(k, s)).toBe(0)
    expect(t.deadTotal).toBe(0)
  })
  it('puts unknown kinds and states after the known ones', () => {
    const t = depthTable({
      notify: { pending: 2, weird: 5 },
      ingest: { dead: 1, pending: 3, snoozed: 4 },
    })
    expect(t.kinds).toEqual(['ingest', 'transcribe', 'analyze', 'notify'])
    expect(t.states).toEqual(['pending', 'running', 'done', 'dead', 'weird', 'snoozed'])
    expect(t.count('ingest', 'pending')).toBe(3)
    expect(t.count('ingest', 'snoozed')).toBe(4)
    expect(t.count('notify', 'weird')).toBe(5)
    expect(t.count('notify', 'dead')).toBe(0)
    expect(t.count('transcribe', 'weird')).toBe(0)
  })
  it('totals dead jobs across every kind, unknown ones too', () => {
    expect(depthTable({ ingest: { dead: 1 }, analyze: { dead: 2 }, notify: { dead: 4 } }).deadTotal).toBe(7)
  })
  it('does not read inherited properties', () => {
    const t = depthTable({ ingest: {} })
    expect(t.count('constructor', 'toString')).toBe(0)
    expect(t.count('ingest', 'toString')).toBe(0)
    expect(t.count('ingest', '__proto__')).toBe(0)
  })
  it('keeps hostile kind names as plain strings', () => {
    const t = depthTable(JSON.parse('{"__proto__": {"pending": 1}, "<b>x</b>": {"dead": 2}}'))
    expect(t.kinds).toContain('<b>x</b>')
    expect(t.deadTotal).toBe(2)
  })
})

describe('isHealth', () => {
  it('accepts the documented shape', () => {
    expect(isHealth({ status: 'ok', queue: {} })).toBe(true)
    expect(isHealth({ status: 'ok', queue: { ingest: { pending: 0, dead: 3 } } })).toBe(true)
  })
  it.each([
    null, [], 'x', 5, {}, { status: 'ok' }, { queue: {} }, { status: 5, queue: {} },
    { status: 'ok', queue: null }, { status: 'ok', queue: [] }, { status: 'ok', queue: { ingest: null } },
    { status: 'ok', queue: { ingest: [] } }, { status: 'ok', queue: { ingest: { pending: '1' } } },
    { status: 'ok', queue: { ingest: { pending: -1 } } }, { status: 'ok', queue: { ingest: { pending: 1.5 } } },
    { status: 'ok', queue: { ingest: { pending: NaN } } }, { status: 'ok', queue: { ingest: { pending: null } } },
    { status: 'ok', queue: { ingest: { pending: Number.MAX_SAFE_INTEGER + 1 } } },
  ])('rejects %j', (v) => {
    expect(isHealth(v)).toBe(false)
  })
})

describe('isJobList', () => {
  const job = {
    id: 1, video_id: 'v', video_title: 't', kind: 'ingest', dedupe_key: 'k', state: 'dead', priority: 0,
    attempts: 1, error_class: 'BUG', last_error: 'e', run_after: '2026-01-01T00:00:00Z', locked_by: null,
    heartbeat_at: null, finished_at: null, created_at: '2026-01-01T00:00:00Z',
  }
  const ok = (over: Record<string, unknown> = {}, next: unknown = null) => ({ items: [{ ...job, ...over }], next_before_id: next })
  it.each([[undefined], [null], ['2026-10-26T10:00:00Z']])('accepts cancel_requested_at %j', (v) => {
    expect(isJobList(ok(v === undefined ? {} : { cancel_requested_at: v }))).toBe(true)
  })
  it('accepts a list and null/positive cursors', () => {
    expect(isJobList({ items: [], next_before_id: null })).toBe(true)
    expect(isJobList(ok())).toBe(true)
    expect(isJobList(ok({}, 7))).toBe(true)
    expect(isJobList(ok({ video_title: null, error_class: null, last_error: null, finished_at: 'x' }))).toBe(true)
  })
  it.each([
    [null], [[]], [{}], [{ items: {}, next_before_id: null }], [{ items: [] }],
    [{ items: [], next_before_id: 0 }], [{ items: [], next_before_id: -1 }], [{ items: [], next_before_id: 1.5 }],
    [{ items: [], next_before_id: '5' }], [{ items: [], next_before_id: Number.MAX_SAFE_INTEGER + 1 }],
    [{ items: [null], next_before_id: null }], [{ items: [[]], next_before_id: null }],
  ])('rejects %j', (v) => {
    expect(isJobList(v)).toBe(false)
  })
  it.each([
    ['id', 0], ['id', -1], ['id', 1.5], ['id', '1'], ['id', null], ['id', Number.MAX_SAFE_INTEGER + 1],
    ['video_id', 1], ['video_id', null],
    ['video_title', 1], ['video_title', undefined],
    ['kind', null], ['kind', 1],
    ['state', null], ['state', 1],
    ['attempts', '1'], ['attempts', null], ['attempts', -1], ['attempts', 1.5],
    ['error_class', 1], ['error_class', undefined],
    ['last_error', 1], ['last_error', undefined],
    ['finished_at', 1], ['finished_at', undefined],
    ['created_at', null], ['created_at', 5],
    ['cancel_requested_at', 1], ['cancel_requested_at', true], ['cancel_requested_at', {}],
  ])('rejects a job whose %s is %j', (field, value) => {
    expect(isJobList(ok({ [field]: value }))).toBe(false)
  })
})

describe('bulk retry helpers (#161)', () => {
  const f = (kind: 'ingest' | 'transcribe' | 'analyze' | null, errorClass: 'BUG' | 'none' | null) =>
    ({ state: 'dead', kind, errorClass }) as const

  it('maps the filter to exactly the strict body', () => {
    expect(bulkRetryBody(f(null, null), true)).toEqual({ state: 'dead', limit: 50, dry_run: true })
    expect(bulkRetryBody(f('analyze', 'BUG'), false)).toEqual({ state: 'dead', kind: 'analyze', error_class: 'BUG', limit: 50, dry_run: false })
    expect(bulkRetryBody(f(null, 'none'), false)).toEqual({ state: 'dead', error_class: 'none', limit: 50, dry_run: false })
  })

  it('names the scope and when transcription is in it', () => {
    expect(bulkScopeLabel(f(null, null))).toBe('all dead jobs')
    expect(bulkScopeLabel(f('ingest', 'none'))).toBe('dead jobs with kind ingest and no error class')
    expect(bulkScopeLabel(f(null, 'BUG'))).toBe('dead jobs with error class BUG')
    expect(bulkIncludesTranscribe(f(null, null))).toBe(true)
    expect(bulkIncludesTranscribe(f('transcribe', null))).toBe(true)
    expect(bulkIncludesTranscribe(f('ingest', null))).toBe(false)
  })

  it('words the result and the fixed errors', () => {
    const o = { retried: 1, job_ids: [1], has_more: false, dry_run: false }
    expect(bulkRetryMessage(o)).toBe('Retried 1 job.')
    expect(bulkRetryMessage({ ...o, retried: 50, has_more: true })).toContain('limit of 50 per call was reached')
    expect(bulkRetryErrorMessage(409)).toBe('Concurrent change, try again')
    expect(bulkRetryErrorMessage(429)).toBe('Too many requests. Try again shortly.')
    expect(bulkRetryErrorMessage(503)).toBe('The database is unavailable. Try again shortly.')
    expect(bulkRetryErrorMessage(422)).toBe('Retry failed')
    expect(bulkRetryErrorMessage(null)).toBe('Retry failed')
  })

  it('guards the response shape', () => {
    expect(isBulkRetryOut({ retried: 0, job_ids: [], has_more: false, dry_run: true })).toBe(true)
    for (const v of [null, {}, { retried: -1, job_ids: [], has_more: false, dry_run: true }, { retried: 1, job_ids: ['1'], has_more: false, dry_run: true }, { retried: 1, job_ids: [1], has_more: 'no', dry_run: true }]) {
      expect(isBulkRetryOut(v)).toBe(false)
    }
  })
})

describe('cancel helpers (#301)', () => {
  const j = (state: string, cancel_requested_at?: string | null, error_class: string | null = null) =>
    ({ state, cancel_requested_at, error_class }) as never

  it('CANCELLED hint is not a failure hint', () => {
    const { label, hint } = errorClassInfo('CANCELLED')
    expect(label).toBe('CANCELLED')
    expect(hint).not.toMatch(/update|fix|error/i)
    expect(ERROR_CLASSES).toContain('CANCELLED')
  })

  it('isCancelling is true only for a running job with a request', () => {
    expect(isCancelling(j('running', '2026-10-26T10:00:00Z'))).toBe(true)
    expect(isCancelling(j('running', null))).toBe(false)
    expect(isCancelling(j('running'))).toBe(false)
    expect(isCancelling(j('done', '2026-10-26T10:00:00Z'))).toBe(false)
    expect(isCancelling(j('pending', '2026-10-26T10:00:00Z'))).toBe(false)
    expect(isCancelling(j('dead', '2026-10-26T10:00:00Z', 'CANCELLED'))).toBe(false)
  })

  it('stateLabel says cancelling, never cancelled', () => {
    expect(stateLabel(j('running', '2026-10-26T10:00:00Z'))).toBe('cancelling')
    expect(stateLabel(j('running', null))).toBe('running')
    expect(stateLabel(j('done', '2026-10-26T10:00:00Z'))).toBe('done')
    expect(stateLabel(j('dead', null, 'CANCELLED'))).toBe('dead')
  })

  it('a CANCELLED dead job has the neutral badge, other dead jobs the danger badge', () => {
    expect(stateBadgeClass(j('dead', null, 'CANCELLED'))).toBe('badge')
    expect(stateBadgeClass(j('dead', null, 'BUG'))).toBe('badge badge--danger')
    expect(stateBadgeClass(j('dead', null, null))).toBe('badge badge--danger')
    expect(stateBadgeClass(j('done'))).toBe('badge badge--success')
    expect(stateBadgeClass(j('running'))).toBe('badge badge--warning')
    expect(stateBadgeClass(j('pending'))).toBe('badge')
  })

  it('canCancel: pending or running with no request only', () => {
    expect(canCancel(j('pending', null))).toBe(true)
    expect(canCancel(j('pending'))).toBe(true)
    expect(canCancel(j('running', null))).toBe(true)
    expect(canCancel(j('running', '2026-10-26T10:00:00Z'))).toBe(false)
    expect(canCancel(j('done'))).toBe(false)
    expect(canCancel(j('dead', null, 'CANCELLED'))).toBe(false)
  })

  it('cancelPath percent-encodes the video id and rejects an empty one', () => {
    expect(cancelPath('abcdefghijk')).toBe('videos/abcdefghijk/cancel')
    expect(cancelPath('a/b?c#d')).toBe('videos/a%2Fb%3Fc%23d/cancel')
    expect(() => cancelPath('')).toThrow()
  })

  const job = (outcome: string) => ({ job_id: 1, kind: 'ingest', outcome })
  const REQUESTED = 'Cancel requested: the job stops at its next checkpoint, which can take about a minute'
  it.each([
    [202, { video_id: 'v', jobs: [job('cancel_requested')] }, REQUESTED],
    [202, { video_id: 'v', jobs: [job('cancel_requested'), job('cancelled')] }, REQUESTED],
    [200, { video_id: 'v', jobs: [job('cancelled')] }, 'Cancelled'],
    [200, { video_id: 'v', jobs: [job('cancelled'), job('cancelled')] }, 'Cancelled'],
    [200, { video_id: 'v', jobs: [] }, 'Nothing to cancel: this video has no active jobs'],
    [404, { detail: 'video not found <b>x</b>' }, 'This video no longer exists'],
    [429, undefined, 'Too many requests. Try again shortly.'],
    [503, undefined, 'The database is unavailable. Try again shortly.'],
    [415, undefined, 'Cancel failed'],
    [422, undefined, 'Cancel failed'],
    [500, { detail: 'secret' }, 'Cancel failed'],
  ])('maps %i %j to a fixed message', (status, body, message) => {
    expect(cancelMessage({ type: 'http', status, body })).toBe(message)
  })
  it('a network failure and an unreadable 2xx body are "Cancel failed"', () => {
    expect(cancelMessage({ type: 'network' })).toBe('Cancel failed')
    expect(cancelMessage({ type: 'http', status: 200, body: 'nope' })).toBe('Cancel failed')
    expect(cancelMessage({ type: 'http', status: 200, body: { jobs: 'x' } })).toBe('Cancel failed')
  })
  it('a 202 whose body is unreadable still says a request was stored', () => {
    expect(cancelMessage({ type: 'http', status: 202, body: undefined })).toBe(REQUESTED)
  })
})

describe('CANCELLED filter and bulk retry (#301)', () => {
  it('?error_class=CANCELLED is accepted and sent', () => {
    const r = parseOpsParams(p('state=dead&error_class=CANCELLED'))
    expect(r.filters.errorClass).toBe('CANCELLED')
    expect(r.corrected).toBeNull()
    expect(buildJobsRequest(r.filters, null).get('error_class')).toBe('CANCELLED')
  })
  it('bulk retry body and scope for CANCELLED', () => {
    const f = { state: 'dead', kind: null, errorClass: 'CANCELLED' } as const
    expect(bulkRetryBody(f, true)).toEqual({ state: 'dead', error_class: 'CANCELLED', limit: 50, dry_run: true })
    expect(bulkRetryBody(f, false).error_class).toBe('CANCELLED')
    expect(bulkScopeLabel(f)).toBe('dead jobs with error class CANCELLED')
  })
})
