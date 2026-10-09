import { formatTimestamp } from '../components/Timestamp'

// Pure logic of the Compare view (#53): URL parameters, default selection,
// time-window alignment and the value formatters. No React, no I/O.

export const NOT_RECORDED = 'not recorded'
export const NO_TIMESTAMP = 'No timestamp'

const ANALYSIS_ID = /^[1-9][0-9]*$/

/**
 * One analysis id from the raw values of a query parameter (the URL is
 * untrusted input). Only a single whole decimal number from 1 to 2^53 - 1
 * with no sign, exponent, fraction, whitespace or leading zero is used;
 * anything else, and any repeated parameter, is dropped.
 */
export function parseAnalysisParam(values: readonly string[]): number | null {
  if (values.length !== 1) return null
  const raw = values[0] as string
  if (!ANALYSIS_ID.test(raw)) return null
  const n = Number(raw)
  return Number.isSafeInteger(n) ? n : null
}

interface PairedRun {
  id: number
  model: string
  prompt_version: string
}

const samePair = (x: PairedRun, y: PairedRun) => x.model === y.model && x.prompt_version === y.prompt_version

/** Newest run (first in API order) whose pair differs from A's; else the newest other than A. */
function defaultB(runs: readonly PairedRun[], a: PairedRun): PairedRun | undefined {
  const others = runs.filter((r) => r.id !== a.id)
  return others.find((r) => !samePair(r, a)) ?? others[0]
}

export interface Selection {
  a: number | null
  b: number | null
  /** A well-formed requested id was not among the loaded runs. */
  unknownRequested: boolean
}

/**
 * Resolve the `a` and `b` parameters against the loaded runs, which are in
 * the API's newest-first order and are never re-sorted here.
 */
export function resolveSelection(
  runs: readonly PairedRun[],
  rawA: readonly string[],
  rawB: readonly string[],
): Selection {
  const known = (id: number | null) => (id === null ? undefined : runs.find((r) => r.id === id))
  const idA = parseAnalysisParam(rawA)
  const idB = parseAnalysisParam(rawB)
  const unknownRequested = (idA !== null && !known(idA)) || (idB !== null && !known(idB))
  let a = known(idA)
  let b = known(idB)

  if (a === undefined && b !== undefined) a = runs.find((r) => r.id !== b?.id)
  if (a === undefined) a = runs[0]
  if (a !== undefined && b !== undefined && a.id === b.id) b = undefined
  if (a !== undefined && b === undefined) b = defaultB(runs, a)
  return { a: a?.id ?? null, b: b?.id ?? null, unknownRequested }
}

/** Does the URL already hold exactly this selection (each of `a` and `b` once, same value)? */
export function selectionMatchesUrl(params: URLSearchParams, a: number | null, b: number | null): boolean {
  const matches = (name: string, id: number | null) => {
    const values = params.getAll(name)
    return id === null ? values.length === 0 : values.length === 1 && values[0] === String(id)
  }
  return matches('a', a) && matches('b', b)
}

/** `$` with four decimals, or "not recorded" for null and non-finite values. */
export function formatCost(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return NOT_RECORDED
  return `$${value.toFixed(4)}`
}

/** `0 s`, `1 min 1 s`, `1 h 2 min 3 s`; whole seconds, rounded down. */
export function formatDuration(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms) || ms < 0) return NOT_RECORDED
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h} h ${m} min ${s} s`
  if (m > 0) return `${m} min ${s} s`
  return `${s} s`
}

function utcIso(value: string): string | null {
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d.toISOString()
}

/** `YYYY-MM-DD HH:MM UTC`; the raw text when the value is not a date. */
export function formatUtcMinutes(value: string): string {
  const iso = utcIso(value)
  return iso === null ? value : `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`
}

/** `YYYY-MM-DD HH:MM:SS UTC`; the raw text when the value is not a date. */
export function formatUtcSeconds(value: string): string {
  const iso = utcIso(value)
  return iso === null ? value : `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`
}

export interface ClaimCounts {
  high: number
  medium: number
  low: number
  notGiven: number
  unknownSpeaker: number
}

/** Confidence is stored text: anything but high, medium or low counts as "not given". */
export function countClaims(claims: readonly { speaker: string; confidence: string | null }[]): ClaimCounts {
  const counts: ClaimCounts = { high: 0, medium: 0, low: 0, notGiven: 0, unknownSpeaker: 0 }
  for (const c of claims) {
    if (c.confidence === 'high') counts.high += 1
    else if (c.confidence === 'medium') counts.medium += 1
    else if (c.confidence === 'low') counts.low += 1
    else counts.notGiven += 1
    if (c.speaker === 'unknown') counts.unknownSpeaker += 1
  }
  return counts
}

export const WINDOW_SEC = 300

/** `0:00–5:00` for window 0; the same labels as the player's timestamps. */
export function windowLabel(k: number, windowSec = WINDOW_SEC): string {
  return `${formatTimestamp(k * windowSec)}–${formatTimestamp((k + 1) * windowSec)}`
}

export interface AlignedRow<T> {
  /** Window index, or null for the final "No timestamp" row. */
  window: number | null
  label: string
  a: T[]
  b: T[]
}

/**
 * Group the items of two runs into time windows `[k*windowSec, (k+1)*windowSec)`.
 * Items without a finite non-negative `start_sec` go in one last row. Windows
 * empty on both sides are left out; within a cell the input order is kept.
 */
export function alignByWindow<T extends { start_sec: number | null }>(
  itemsA: readonly T[],
  itemsB: readonly T[],
  windowSec = WINDOW_SEC,
): AlignedRow<T>[] {
  if (!Number.isFinite(windowSec) || windowSec <= 0) throw new RangeError('windowSec must be positive')
  const byWindow = new Map<number, AlignedRow<T>>()
  const untimed: AlignedRow<T> = { window: null, label: NO_TIMESTAMP, a: [], b: [] }
  const place = (items: readonly T[], side: 'a' | 'b') => {
    for (const item of items) {
      const t = item.start_sec
      if (t === null || !Number.isFinite(t) || t < 0) {
        untimed[side].push(item)
        continue
      }
      const k = Math.floor(t / windowSec)
      let row = byWindow.get(k)
      if (row === undefined) {
        row = { window: k, label: windowLabel(k, windowSec), a: [], b: [] }
        byWindow.set(k, row)
      }
      row[side].push(item)
    }
  }
  place(itemsA, 'a')
  place(itemsB, 'b')
  const rows = [...byWindow.values()].sort((x, y) => (x.window as number) - (y.window as number))
  if (untimed.a.length > 0 || untimed.b.length > 0) rows.push(untimed)
  return rows
}
