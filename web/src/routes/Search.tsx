import { Fragment, useEffect, useId, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { formatTimestamp } from "../components/Timestamp";
import { SearchError, useSearch, type SearchPage } from "../hooks/useSearch";
import { publishedDate } from "../lib/library";
import {
  excerptFragments,
  MAX_QUERY_CHARS,
  nextEnabled,
  parseSearchParams,
  searchHref,
  timestampHref,
  validateQuery,
} from "../lib/search";

type Result = SearchPage["results"][number];

// Plain semantic HTML on purpose: styling is #131. The query and page live in
// the URL only; TanStack Query owns the server state (architecture §9).
// Everything shown is a React text node: stored text is never parsed as HTML.
export function Search() {
  const [params, setParams] = useSearchParams();
  const { q, page, corrected } = parseSearchParams(params);

  // Invalid q/page are dropped from the URL (replace, not push). This render
  // already ignores them, so nothing invalid is ever requested or shown.
  const fix = corrected === null ? null : corrected.toString();
  useEffect(() => {
    if (fix !== null) setParams(new URLSearchParams(fix), { replace: true });
  }, [fix, setParams]);

  const query = useSearch(q, page);
  const headingRef = useRef<HTMLHeadingElement>(null);

  const goToPage = (target: number) => {
    if (q === null) return;
    setParams(new URLSearchParams(searchHref(q, target)));
    // The results stay mounted while the next page loads, so the heading exists.
    headingRef.current?.focus();
  };

  const submit = (next: string) => {
    const target = next === "" ? "" : searchHref(next, 1);
    if (target !== params.toString()) setParams(new URLSearchParams(target));
  };

  const data = q === null ? undefined : query.data;
  const placeholder = query.isPlaceholderData;
  const loading =
    q !== null && query.isFetching && (data === undefined || placeholder);
  const failed = q !== null && query.isError && !query.isFetching;
  const pastEnd =
    data !== undefined && data.results.length === 0 && data.offset > 0;
  const noResults =
    data !== undefined && data.results.length === 0 && data.offset === 0;

  let liveText = "";
  if (failed) liveText = errorText(query.error);
  else if (pastEnd) liveText = "No results on this page";
  else if (noResults) liveText = "No transcripts match this search";
  else if (data !== undefined)
    liveText = `Results ${data.offset + 1}–${data.offset + data.results.length}`;

  return (
    <section className="page search">
      <h1>Search</h1>

      <SearchForm key={q ?? ""} initial={q ?? ""} onSubmit={submit} />

      {q === null ? (
        <p className="muted">Search what was said in processed videos</p>
      ) : (
        <>
          <h2 ref={headingRef} tabIndex={-1}>
            Results
          </h2>
          <p role="status" aria-live="polite" className="muted">
            {liveText}
          </p>
          {loading && <p>Searching…</p>}
          {failed &&
            !(
              query.error instanceof SearchError && query.error.status === 422
            ) && (
              <button
                type="button"
                className="btn"
                onClick={() => void query.refetch()}
              >
                Retry
              </button>
            )}
          {noResults && (
            <p>
              Common words (like “the”) are ignored, and a search needs at least
              one word that is not excluded with -.
            </p>
          )}
          {pastEnd && (
            <p>
              <Link
                to={{ search: searchHref(q, 1) }}
                onClick={() => headingRef.current?.focus()}
              >
                Go to page 1
              </Link>
            </p>
          )}
          {data !== undefined && data.results.length > 0 && (
            <>
              <ol className="search__results">
                {data.results.map((r) => (
                  <ResultItem key={r.video_id} result={r} />
                ))}
              </ol>
              <nav aria-label="Pagination" className="pagination">
                <button
                  type="button"
                  className="btn"
                  disabled={page <= 1 || placeholder || query.isFetching}
                  onClick={() => goToPage(page - 1)}
                >
                  Previous
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={
                    !nextEnabled(page, data.has_more) ||
                    placeholder ||
                    query.isFetching
                  }
                  onClick={() => goToPage(page + 1)}
                >
                  Next
                </button>
              </nav>
            </>
          )}
        </>
      )}
    </section>
  );
}

function errorText(error: unknown): string {
  if (error instanceof SearchError) {
    if (error.status === 503)
      return "Search is unavailable right now. Try again shortly.";
    if (error.status === 422)
      return "This search could not be run. Try shorter or different words.";
  }
  return "Could not search";
}

// Keyed by the URL's query, so Back/Forward and a deep link reset the box while
// typing (which does not change the URL) never does.
function SearchForm({
  initial,
  onSubmit,
}: {
  initial: string;
  onSubmit: (q: string) => void;
}) {
  const [draft, setDraft] = useState(initial);
  const [problem, setProblem] = useState<string | null>(null);
  const inputId = useId();
  const problemId = useId();

  return (
    <form
      role="search"
      className="search__form"
      onSubmit={(e) => {
        e.preventDefault();
        const check = validateQuery(draft);
        if (check.ok) {
          setProblem(null);
          setDraft(check.q);
          onSubmit(check.q);
        } else if (check.reason === "empty") {
          setProblem(null);
          setDraft("");
          onSubmit("");
        } else {
          setProblem(
            check.reason === "too_long"
              ? `Search terms are limited to ${MAX_QUERY_CHARS} characters`
              : "Search terms cannot contain a NUL character",
          );
        }
      }}
    >
      <div className="filters">
        <div className="field search__field">
          <label htmlFor={inputId}>Search transcripts</label>
          <input
            id={inputId}
            type="search"
            className="input"
            name="q"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-invalid={problem !== null}
            aria-describedby={problem === null ? undefined : problemId}
          />
        </div>
        <button type="submit" className="btn btn--primary">
          Search
        </button>
      </div>
      {problem !== null && (
        <p id={problemId} role="alert" className="alert alert--danger">
          {problem}
        </p>
      )}
    </form>
  );
}

function nonEmpty(s: string | null): string | null {
  return s === null || s.trim() === "" ? null : s;
}

function ResultItem({ result: r }: { result: Result }) {
  const fragments = excerptFragments(r.excerpts);
  const channel =
    nonEmpty(r.channel_title) ?? nonEmpty(r.channel_id) ?? "Unknown channel";
  return (
    <li className="search__result card">
      <h3 className="search__title">
        <Link to={`/videos/${encodeURIComponent(r.video_id)}`}>
          {nonEmpty(r.title) ?? r.video_id}
        </Link>
      </h3>
      <div className="muted">
        <span>{channel}</span> ·{" "}
        <span>{publishedDate(r.published_at) ?? "Date unknown"}</span>
        {r.unavailable !== null && (
          <>
            {" "}
            · <span>Unavailable on YouTube</span>
          </>
        )}
      </div>
      {fragments.length > 0 && (
        <p>
          {fragments.map((fragment, i) => (
            <Fragment key={i}>
              {i > 0 && " … "}
              {fragment.spans.map((span, j) =>
                span.match ? (
                  <mark key={j}>{span.text}</mark>
                ) : (
                  <Fragment key={j}>{span.text}</Fragment>
                ),
              )}
              <TimestampLink
                videoId={r.video_id}
                startSec={fragment.startSec}
              />
            </Fragment>
          ))}
        </p>
      )}
    </li>
  );
}

// The link form of a timestamp: opens the detail view at that moment (#110).
// #129 owns the `?t=` route and should take this over with its own helper.
function TimestampLink({
  videoId,
  startSec,
}: {
  videoId: string;
  startSec: number | null;
}) {
  const href = timestampHref(videoId, startSec);
  if (href === null || startSec === null) return null;
  const label = formatTimestamp(startSec);
  return (
    <>
      {" "}
      <Link to={href} className="timestamp" aria-label={`Open at ${label}`}>
        <time>{label}</time>
      </Link>
    </>
  );
}
