/** The server's cap on one POST /videos/batch (MAX_BATCH_SUBMIT, #341): the one web-side copy. */
export const MAX_BATCH_SUBMIT = 200;

export type BatchOutcome =
  | "queued"
  | "restarted"
  | "already_submitted"
  | "duplicate"
  | "invalid"
  | "cancellation_in_progress"
  | "failed";

/** An outcome the person can resend: the request failed, or the video is still being deleted. */
export const outcomeNeedsRetry = (outcome: BatchOutcome) =>
  outcome === "failed" || outcome === "cancellation_in_progress";

/** The polite summary of a batch (#240, #343): `Queued N · Already added M · Not valid K`, non-zero parts only. */
export function summarizeOutcomes(outcomes: readonly BatchOutcome[]): string {
  const count = (...wanted: BatchOutcome[]) =>
    outcomes.filter((o) => wanted.includes(o)).length;
  return [
    ["Queued", count("queued", "restarted")],
    ["Already added", count("already_submitted")],
    ["Not valid", count("invalid")],
  ]
    .filter(([, n]) => n !== 0)
    .map(([label, n]) => `${label} ${n}`)
    .join(" · ");
}
