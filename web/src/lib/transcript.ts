// Pure helpers for the Transcript view (#51): URL page parsing (the URL is
// untrusted input), the source label and the speaker-label rule. No React.

export const PAGE_SIZE = 200;
// #42 rejects an offset above this.
export const MAX_OFFSET = 1_000_000;
/** The highest page whose offset the API accepts. */
export const MAX_PAGE = MAX_OFFSET / PAGE_SIZE + 1;

const WHOLE_NUMBER = /^[1-9][0-9]{0,9}$/;

/**
 * The `page` URL parameter. Anything but a canonical whole number in
 * 1..MAX_PAGE (including a repeated parameter) is `invalid`: the caller
 * renders page 1 and drops the parameter from the URL.
 */
export function parsePage(params: URLSearchParams): {
  page: number;
  invalid: boolean;
} {
  const all = params.getAll("page");
  if (all.length === 0) return { page: 1, invalid: false };
  const raw = all[0];
  if (all.length === 1 && raw !== undefined && WHOLE_NUMBER.test(raw)) {
    const n = Number(raw);
    if (n <= MAX_PAGE) return { page: n, invalid: false };
  }
  return { page: 1, invalid: true };
}

/** Number of pages for `total` segments: at least 1, never past the offset cap. */
export function lastPage(total: number): number {
  return Math.min(Math.max(1, Math.ceil(total / PAGE_SIZE)), MAX_PAGE);
}

/** Groups of three digits separated by a space: 2500 -> "2 500". */
export function formatCount(n: number): string {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

const SOURCE_LABELS = new Map([
  ["youtube_manual", "YouTube captions (manual)"],
  ["youtube_auto", "YouTube auto-generated captions"],
  ["whisper", "Machine transcription"],
]);

/** Known sources get a plain-language label; any other value is shown as stored. */
export function sourceLabel(source: string): string {
  return SOURCE_LABELS.get(source) ?? source;
}

export function languageLabel(language: string | null): string {
  return language === null ? "Language unknown" : language;
}

function speakerOf(s: { speaker: string | null }): string | null {
  return s.speaker === null || s.speaker.trim() === "" ? null : s.speaker;
}

/**
 * Per segment, the speaker to print or `null` for no label. A label appears on
 * the first segment and wherever the speaker differs from the previous
 * segment's; a missing speaker counts as a value, so `A, A, null, A` labels the
 * 1st and 4th segments.
 */
export function speakerLabels(
  segments: ReadonlyArray<{ speaker: string | null }>,
): Array<string | null> {
  let previous: string | null = null;
  return segments.map((seg, i) => {
    const current = speakerOf(seg);
    const label =
      current !== null && (i === 0 || current !== previous) ? current : null;
    previous = current;
    return label;
  });
}
