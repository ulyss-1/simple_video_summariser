import type { UseQueryResult } from "@tanstack/react-query";
import {
  AnalysisEstimateApiError,
  type AnalysisEstimateData,
  type ModelEstimateData,
} from "../hooks/useAnalysisEstimate";
import { formatCost } from "../lib/compare";
import { Section } from "./AnalysisView";

/** The id the Re-analyse button points `aria-describedby` at. */
export const ESTIMATE_BODY_ID = "analysis-estimate-body";

const REASONS: Record<string, string> = {
  no_transcript: "No transcript yet",
  no_cost_samples: "No recorded costs for this model yet",
  no_price: "No price known for this model",
};

/** A 4xx will not change on a retry; anything else may. */
const retryable = (error: unknown) =>
  !(
    error instanceof AnalysisEstimateApiError &&
    error.status >= 400 &&
    error.status < 500
  );

function Row({ row, batch }: { row: ModelEstimateData; batch: boolean }) {
  if (row.state === "estimated" && row.cost_usd !== null) {
    return (
      <li>
        {`${row.model}: about ${formatCost(row.cost_usd)} (estimate, USD)`}
        <br />
        <small className="muted">
          {`Based on the median of the last ${row.samples} recorded analyses${batch ? " · batch pricing" : ""}`}
        </small>
      </li>
    );
  }
  if (row.state === "unknown") {
    return (
      <li>
        {`${row.model}: no estimate`}
        <br />
        <small className="muted">
          {(row.reason && REASONS[row.reason]) ?? "No estimate available"}
        </small>
      </li>
    );
  }
  return <li>{`${row.model}: no API cost`}</li>;
}

/**
 * The expected cost of the LLM analysis of this video, per model (#153). The
 * state of every row comes typed from the API; nothing here computes a price.
 * A failure stays inside this section: the rest of the page is unaffected.
 */
export function AnalysisEstimate({
  query,
}: {
  query: UseQueryResult<AnalysisEstimateData>;
}) {
  const data = query.data;
  return (
    <Section title="Estimated analysis cost">
      <div id={ESTIMATE_BODY_ID}>
        {data !== undefined ? (
          <ul>
            {data.estimates.map((row) => (
              <Row key={row.model} row={row} batch={data.batch} />
            ))}
          </ul>
        ) : query.isError ? (
          <div role="alert">
            <p>Could not load the cost estimate</p>
            {retryable(query.error) && (
              <button
                className="btn"
                type="button"
                onClick={() => {
                  void query.refetch();
                }}
              >
                Retry
              </button>
            )}
          </div>
        ) : (
          <p role="status">Loading cost estimate…</p>
        )}
      </div>
    </Section>
  );
}
