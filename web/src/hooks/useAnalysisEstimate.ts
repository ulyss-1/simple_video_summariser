import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { components } from "../api";
import { api, rethrowHttp } from "../api";

export type AnalysisEstimateData = components["schemas"]["AnalysisEstimate"];
export type ModelEstimateData = components["schemas"]["ModelEstimate"];

/** A non-2xx response. Carries only the status: the body is never surfaced. */
export class AnalysisEstimateApiError extends Error {
  readonly status: number;
  constructor(status: number) {
    super(`HTTP ${status}`);
    this.name = "AnalysisEstimateApiError";
    this.status = status;
  }
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * The typed encoding of #153: a number only when `estimated`, a reason only when
 * `unknown`. A row that breaks it is a bad response, never shown as a guess.
 */
function isModelEstimate(v: unknown): v is ModelEstimateData {
  if (
    !isRecord(v) ||
    typeof v.model !== "string" ||
    typeof v.samples !== "number"
  )
    return false;
  switch (v.state) {
    case "estimated":
      return (
        typeof v.cost_usd === "number" &&
        Number.isFinite(v.cost_usd) &&
        v.cost_usd >= 0 &&
        v.reason === null
      );
    case "unknown":
      return (
        v.cost_usd === null &&
        (v.reason === "no_transcript" ||
          v.reason === "no_cost_samples" ||
          v.reason === "no_price")
      );
    case "free":
      return v.cost_usd === null && v.reason === null;
    default:
      return false;
  }
}

function isEstimate(v: unknown): v is AnalysisEstimateData {
  return (
    isRecord(v) &&
    typeof v.batch === "boolean" &&
    Array.isArray(v.estimates) &&
    v.estimates.every(isModelEstimate)
  );
}

async function fetchEstimate(
  videoId: string,
  signal: AbortSignal,
): Promise<AnalysisEstimateData> {
  const body: unknown = await api
    .get("/videos/{video_id}/analysis-estimate", {
      params: { path: { video_id: videoId } },
      signal,
    })
    .catch((error: unknown) =>
      rethrowHttp(error, (_e, status) => new AnalysisEstimateApiError(status)),
    );
  if (!isEstimate(body)) throw new Error("Unexpected response shape");
  return body;
}

/**
 * GET /api/videos/{id}/analysis-estimate: the estimated USD cost of analysing the
 * video, per model. Pass the id only after `parseVideoId`; `null` makes no
 * request. Keyed under `['video', id, ...]`, so the invalidation that follows a
 * re-analysis refreshes it with the video. `latestAnalysisAt` is part of the key:
 * a finished analysis records a new cost sample, and the video poll (#50) is what
 * sees it, so the estimate is asked again then. The previous figure stays on
 * screen meanwhile. No polling of its own.
 */
export function useAnalysisEstimate(videoId: string | null, latestAnalysisAt: string | null = null) {
  return useQuery({
    queryKey: ["video", videoId, "analysis-estimate", latestAnalysisAt],
    queryFn: ({ signal }) => fetchEstimate(videoId as string, signal),
    enabled: videoId !== null,
    placeholderData: keepPreviousData,
  });
}
