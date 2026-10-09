export interface RosterSpeaker {
  name: string
  role: string | null
}

/**
 * Speakers from the untrusted `speaker_roster` JSON (LLM output). Never
 * throws; same rules as the server-side renderer (services/api/render.py).
 */
export function parseRoster(roster: unknown): RosterSpeaker[] {
  if (typeof roster !== 'object' || roster === null || Array.isArray(roster)) return []
  const speakers = (roster as Record<string, unknown>).speakers
  if (!Array.isArray(speakers)) return []
  const out: RosterSpeaker[] = []
  for (const entry of speakers as unknown[]) {
    if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) continue
    const { name, role } = entry as Record<string, unknown>
    if (typeof name !== 'string' || name.trim() === '') continue
    out.push({ name, role: typeof role === 'string' && role !== '' ? role : null })
  }
  return out
}
