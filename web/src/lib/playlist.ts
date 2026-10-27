// Pure helpers for the playlist import page (#343): the response guard, the
// fixed error texts and the row labels. No React, no network.
import { ApiError, type components } from "../api";
import { statusFilterLabel, type VideoStatus } from "./library";
import { parseVideoId } from "./videoId";

export type PlaylistListing = components["schemas"]["PlaylistResolveResponse"];
export type PlaylistEntry = components["schemas"]["PlaylistEntryOut"];

/** The server lists at most this many entries (MAX_PLAYLIST_ENTRIES, #342). */
export const MAX_PLAYLIST_ENTRIES = 200;

/** A longer search param is dropped, not sent (7.3). */
export const MAX_PLAYLIST_URL_CHARS = 2000;

const STATUSES = new Set([
  "done",
  "processing",
  "unavailable",
  "failed",
  "idle",
  "deleted",
]);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

function isEntry(value: unknown): value is PlaylistEntry {
  return (
    isRecord(value) &&
    typeof value.video_id === "string" &&
    parseVideoId(value.video_id) !== null &&
    typeof value.position === "number" &&
    (value.title === null || typeof value.title === "string") &&
    typeof value.url === "string" &&
    typeof value.known === "boolean" &&
    (value.processing_status === null ||
      (typeof value.processing_status === "string" &&
        STATUSES.has(value.processing_status)))
  );
}

export function isPlaylistListing(value: unknown): value is PlaylistListing {
  return (
    isRecord(value) &&
    typeof value.playlist_id === "string" &&
    (value.title === null || typeof value.title === "string") &&
    (value.total_count === null || typeof value.total_count === "number") &&
    typeof value.truncated === "boolean" &&
    Array.isArray(value.entries) &&
    value.entries.every(isEntry)
  );
}

export const PLAYLIST_URL_HELP =
  "Enter a public YouTube playlist URL. Mixes, Liked videos and Watch later cannot be imported.";

/** Fixed text per failure; nothing from the response is shown. */
export function playlistErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === "http") {
    switch (error.status) {
      case 422:
        return PLAYLIST_URL_HELP;
      case 404:
        return "Playlist not found or not public";
      case 503:
        return "YouTube is unavailable or limiting requests. Try again later.";
      case 502:
        return "Could not read the playlist. Try again later.";
      case 429:
        return "Too many requests. Try again shortly.";
    }
  }
  return "Could not load the playlist. Check your connection and try again.";
}

export const playlistTitle = (listing: PlaylistListing): string =>
  listing.title === null || listing.title.trim() === ""
    ? "Untitled playlist"
    : listing.title;

export const entryName = (entry: PlaylistEntry): string =>
  entry.title === null || entry.title === "" ? entry.video_id : entry.title;

/** The count line: "{n} videos", or the cap notice when the playlist was cut. */
export function playlistCountLine(listing: PlaylistListing): string {
  if (listing.truncated) {
    return listing.total_count === null
      ? `Showing the first ${MAX_PLAYLIST_ENTRIES} videos`
      : `Showing the first ${MAX_PLAYLIST_ENTRIES} of ${listing.total_count} videos`;
  }
  const n = listing.entries.length;
  return `${n} video${n === 1 ? "" : "s"}`;
}

/**
 * "Already known · {status}" for a known entry. A tombstone reads `Deleted`. A
 * video queued from this page and still waiting reads `Queued`, as in the
 * result list; any other known video shows its Library status.
 */
export function knownEntryLabel(
  entry: PlaylistEntry,
  queuedHere: boolean,
): string {
  const status = entry.processing_status;
  if (status === null) return "Already known";
  if (status === "deleted") return "Already known · Deleted";
  if (queuedHere && (status === "processing" || status === "idle"))
    return "Already known · Queued";
  return `Already known · ${statusFilterLabel(status satisfies VideoStatus)}`;
}
