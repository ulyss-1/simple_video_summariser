import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { Player } from "../components/Player";
import { Timestamp } from "../components/Timestamp";
import { TranscriptApiError, useTranscript } from "../hooks/useTranscript";
import { useVideo } from "../hooks/useVideo";
import {
  formatCount,
  languageLabel,
  lastPage,
  MAX_PAGE,
  PAGE_SIZE,
  parsePage,
  sourceLabel,
  speakerLabels,
} from "../lib/transcript";
import { parseVideoId } from "../lib/videoId";

// Plain semantic HTML on purpose: styling is #130. The page number lives in the
// URL only; TanStack Query owns the server state (architecture §9). Every
// string from the API or the URL is a React text child, never markup.
export function Transcript() {
  const videoId = parseVideoId(useParams().videoId);
  if (videoId === null) return <VideoNotFound />;
  // Keyed by id: a different video starts from a clean slate, so the previous
  // video's segments can never be kept as placeholder data.
  return <TranscriptView key={videoId} videoId={videoId} />;
}

function VideoNotFound() {
  return (
    <section>
      <h1>Video not found</h1>
      <p>
        <Link to="/">Library</Link>
      </p>
    </section>
  );
}

function TranscriptView({ videoId }: { videoId: string }) {
  const [params, setParams] = useSearchParams();
  const { page, invalid } = parsePage(params);

  // An invalid page is dropped from the URL (replace, not push). This render
  // already uses page 1, so the invalid value is never requested.
  useEffect(() => {
    if (!invalid) return;
    const next = new URLSearchParams(params);
    next.delete("page");
    setParams(next, { replace: true });
  }, [invalid, params, setParams]);

  const video = useVideo(videoId);
  const q = useTranscript(videoId, page);
  const data = q.data;
  const switching = q.isPlaceholderData;

  const rawTitle = video.data?.title;
  const title = rawTitle != null && rawTitle.trim() !== "" ? rawTitle : videoId;
  useEffect(() => {
    document.title = `${title} – Transcript`;
  }, [title]);

  // A transcript replaced between two pages (D2: a better one won).
  const [shownId, setShownId] = useState<number | null>(null);
  const [replaced, setReplaced] = useState(false);
  if (data !== undefined && !switching && data.transcript_id !== shownId) {
    setShownId(data.transcript_id);
    if (shownId !== null) setReplaced(true);
  }

  // After a page change, focus moves to the top of the list.
  const listHeading = useRef<HTMLHeadingElement>(null);
  const shownPage = useRef<number | null>(null);
  const settled = data !== undefined && !switching;
  useEffect(() => {
    if (!settled) return;
    if (shownPage.current !== null && shownPage.current !== page)
      listHeading.current?.focus();
    shownPage.current = page;
  }, [settled, page]);

  const goTo = (target: number) => {
    const next = new URLSearchParams(params);
    if (target <= 1) next.delete("page");
    else next.set("page", String(target));
    setParams(next);
  };

  const error = q.isError ? q.error : null;
  const videoMissing =
    error instanceof TranscriptApiError &&
    error.status === 404 &&
    error.notFound === "video";
  const noTranscript =
    error instanceof TranscriptApiError &&
    error.status === 404 &&
    error.notFound === "transcript";

  if (videoMissing) return <VideoNotFound />;

  return (
    <section>
      <h1>{title}</h1>
      <p>
        <Link to={`/videos/${videoId}`}>Back to video</Link>
      </p>
      {q.status !== "pending" && (
        <Player videoId={videoId} title={rawTitle ?? undefined} />
      )}

      {error !== null ? (
        noTranscript ? (
          <p>No transcript yet for this video</p>
        ) : (
          <div role="alert">
            <p>
              {error instanceof TranscriptApiError && error.status === 503
                ? "The database is unavailable. Try again shortly."
                : "Could not load the transcript"}
            </p>
            <button type="button" onClick={() => void q.refetch()}>
              Retry
            </button>
          </div>
        )
      ) : data === undefined ? (
        <p role="status">Loading transcript…</p>
      ) : (
        <Segments
          data={data}
          page={page}
          switching={switching}
          replaced={replaced}
          headingRef={listHeading}
          goTo={goTo}
          lastPageSearch={pageSearch(params, lastPage(data.total_segments))}
        />
      )}
    </section>
  );
}

function pageSearch(params: URLSearchParams, page: number): string {
  const next = new URLSearchParams(params);
  if (page <= 1) next.delete("page");
  else next.set("page", String(page));
  return next.toString();
}

function Segments({
  data,
  page,
  switching,
  replaced,
  headingRef,
  goTo,
  lastPageSearch,
}: {
  data: NonNullable<ReturnType<typeof useTranscript>["data"]>;
  page: number;
  switching: boolean;
  replaced: boolean;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
  goTo: (page: number) => void;
  lastPageSearch: string;
}) {
  const total = data.total_segments;
  const segments = data.segments;
  const offset = (page - 1) * PAGE_SIZE;
  const pages = lastPage(total);
  const hasNext = offset + segments.length < total && page < MAX_PAGE;
  const labels = speakerLabels(segments);

  return (
    <>
      <p>
        {sourceLabel(data.source)} · {languageLabel(data.language)}
      </p>
      {replaced && (
        <p role="status">This transcript was updated while you were reading</p>
      )}

      <h2 ref={headingRef} tabIndex={-1}>
        Segments
      </h2>
      <p aria-live="polite">
        {segments.length > 0
          ? `Segments ${formatCount(offset + 1)}–${formatCount(offset + segments.length)} of ${formatCount(total)}`
          : ""}
      </p>
      {switching && <p role="status">Loading…</p>}

      {total === 0 && <p>This transcript is empty</p>}
      {total > 0 && segments.length === 0 && (
        <p>
          <span>No segments on this page</span>{" "}
          <Link to={{ search: lastPageSearch }}>Go to the last page</Link>
        </p>
      )}

      {segments.length > 0 && (
        <ol start={offset + 1}>
          {segments.map((s, i) => {
            const speaker = labels[i];
            return (
              <li key={s.index}>
                <Timestamp seconds={s.start} />{" "}
                {speaker != null && (
                  <span className="speaker">{speaker}: </span>
                )}
                <span>{s.text}</span>
              </li>
            );
          })}
        </ol>
      )}

      {total > 0 && (
        <nav aria-label="Pagination">
          <button
            type="button"
            disabled={page <= 1 || switching}
            onClick={() => goTo(page - 1)}
          >
            Previous
          </button>{" "}
          <span>
            Page {page} of {pages}
          </span>{" "}
          <button
            type="button"
            disabled={!hasNext || switching}
            onClick={() => goTo(page + 1)}
          >
            Next
          </button>
        </nav>
      )}
    </>
  );
}
