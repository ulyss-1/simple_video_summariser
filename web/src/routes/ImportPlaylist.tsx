import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";
import { usePlaylist, useRefetchPlaylist } from "../hooks/usePlaylist";
import {
  batchSubmitErrorMessage,
  useSubmitVideosBatch,
} from "../hooks/useSubmitVideosBatch";
import {
  outcomeNeedsRetry,
  summarizeOutcomes,
  type BatchOutcome,
} from "../lib/batchSubmit";
import { canonicalVideoUrl } from "../lib/channelCatalog";
import {
  entryName,
  knownEntryLabel,
  MAX_PLAYLIST_URL_CHARS,
  PLAYLIST_URL_HELP,
  playlistCountLine,
  playlistErrorMessage,
  playlistTitle,
  type PlaylistEntry,
  type PlaylistListing,
} from "../lib/playlist";
import { countLabel, toggleInSet } from "../lib/selection";
import { parseVideoId } from "../lib/videoId";

/** A `url` search param that is worth sending, or null (7.3: invalid params are dropped). */
function parseUrlParam(raw: string | null): string | null {
  if (raw === null) return null;
  const trimmed = raw.trim();
  return trimmed === "" || trimmed.length > MAX_PLAYLIST_URL_CHARS
    ? null
    : trimmed;
}

// Paste a public playlist URL, see its videos (at most 200) and queue a
// hand-picked subset through POST /videos/batch (#343). The URL lives in the
// search params; the selection is local to the listing. Everything from the
// API renders as a React text child.
export function ImportPlaylist() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("url");
  const url = parseUrlParam(raw);
  const [value, setValue] = useState(url ?? "");
  const [local, setLocal] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  // The trimmed text that was sent. It stays set after a success, so the same
  // text cannot be sent again; it is cleared when the text changes or a
  // request fails.
  const latchRef = useRef<string | null>(url);
  const query = usePlaylist(url);
  const inputId = useId();
  const hintId = useId();
  const errorId = useId();

  useEffect(() => {
    if (raw !== null && url === null) {
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.delete("url");
          return next;
        },
        { replace: true },
      );
    }
  }, [raw, url, setParams]);

  const failed = query.isError;
  useEffect(() => {
    if (failed) {
      latchRef.current = null;
      inputRef.current?.focus();
    }
  }, [failed, query.errorUpdatedAt]);

  const message = local
    ? PLAYLIST_URL_HELP
    : failed
      ? playlistErrorMessage(query.error)
      : null;
  const loading = query.isFetching && query.data === undefined;

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = value.trim();
    if (text === "" || text.length > MAX_PLAYLIST_URL_CHARS) {
      setLocal(true);
      return;
    }
    if (latchRef.current === text) return;
    latchRef.current = text;
    if (text === url) void query.refetch();
    else setParams({ url: text });
  };

  return (
    <section className="page">
      <h1>Import a playlist</h1>
      <p className="muted">
        <Link to="/">Back to Library</Link>
      </p>
      <form className="card" onSubmit={onSubmit}>
        <label className="field" htmlFor={inputId}>
          YouTube playlist URL
        </label>
        <input
          id={inputId}
          ref={inputRef}
          className="input"
          type="text"
          value={value}
          aria-invalid={message === null ? undefined : true}
          aria-describedby={message === null ? hintId : `${hintId} ${errorId}`}
          onChange={(event) => {
            setValue(event.target.value);
            latchRef.current = null;
            setLocal(false);
          }}
        />
        <p id={hintId} className="muted">
          A public playlist, or a video link that carries one. At most 200
          videos are listed.
        </p>
        <button className="btn btn--primary" type="submit" disabled={loading}>
          Show videos
        </button>
        {loading && (
          <p role="status">Loading playlist… This can take up to a minute.</p>
        )}
        {message !== null && (
          <p id={errorId} className="alert alert--danger" role="alert">
            {message}
          </p>
        )}
      </form>
      {url !== null && query.data !== undefined && (
        <Listing key={url} url={url} listing={query.data} />
      )}
    </section>
  );
}

interface ResultRow {
  videoId: string;
  outcome: BatchOutcome;
}

function Listing({ url, listing }: { url: string; listing: PlaylistListing }) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [rows, setRows] = useState<ResultRow[] | null>(null);
  const mutation = useSubmitVideosBatch();
  const refetchPlaylist = useRefetchPlaylist(url);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const actionRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef(false);
  // Synchronous guards: a handler from an older render sees these, so the
  // window between a settled request and the re-render cannot send twice.
  // The latch stays set after a success and is released only by an error or
  // by a change of the selection.
  const latchRef = useRef(false);
  const rowsRef = useRef<ResultRow[] | null>(null);
  const busyRef = useRef(false);
  const pending = mutation.isPending;

  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
    else if (returnFocusRef.current) {
      returnFocusRef.current = false;
      actionRef.current?.focus();
    }
  }, [confirming]);

  const entries = listing.entries;
  const newIds = entries.filter((e) => !e.known).map((e) => e.video_id);
  const selectedIds = newIds.filter((id) => selected.has(id));
  const count = selectedIds.length;
  const queuedHere = new Set(
    rows
      ?.filter((r) => r.outcome === "queued" || r.outcome === "restarted")
      .map((r) => r.videoId),
  );

  const changeSelection = (
    next: (current: ReadonlySet<string>) => Set<string>,
  ) => {
    latchRef.current = false;
    mutation.reset();
    setSelected(next);
  };

  const closeConfirm = () => {
    returnFocusRef.current = true;
    setConfirming(false);
  };

  const send = (
    ids: string[],
    merge: (response: ResultRow[]) => ResultRow[],
    onDone: () => void,
  ) => {
    busyRef.current = true;
    mutation.mutate(ids, {
      onSuccess: (response) => {
        const fresh: ResultRow[] = ids.map((videoId, index) => ({
          videoId,
          outcome:
            response.results.find((r) => r.index === index)?.outcome ??
            "failed",
        }));
        const next = merge(fresh);
        rowsRef.current = next;
        setRows(next);
        onDone();
        void refetchPlaylist();
      },
      onError: () => {
        latchRef.current = false;
      },
      onSettled: () => {
        busyRef.current = false;
        returnFocusRef.current = true;
        setConfirming(false);
      },
    });
  };

  const confirm = () => {
    if (latchRef.current || busyRef.current || pending || count === 0) return;
    latchRef.current = true;
    const ids = selectedIds;
    send(
      ids,
      (fresh) => fresh,
      // Every submitted id has a result; only now is the selection released.
      () =>
        setSelected(
          (current) => new Set([...current].filter((id) => !ids.includes(id))),
        ),
    );
  };

  const retry = () => {
    const before = rowsRef.current;
    if (busyRef.current || pending || before === null || before !== rows)
      return;
    const again = before
      .filter((r) => outcomeNeedsRetry(r.outcome))
      .map((r) => r.videoId);
    if (again.length === 0) return;
    send(
      again,
      (fresh) =>
        before.map(
          (row) => fresh.find((f) => f.videoId === row.videoId) ?? row,
        ),
      () => undefined,
    );
  };

  return (
    <>
      <h2>{playlistTitle(listing)}</h2>
      <p>{playlistCountLine(listing)}</p>

      {entries.length === 0 ? (
        <p>This playlist has no videos</p>
      ) : (
        <>
          <div className="card" role="group" aria-label="Selection">
            <div className="row">
              <button
                className="btn"
                type="button"
                disabled={pending || newIds.length === 0}
                onClick={() => changeSelection(() => new Set(newIds))}
              >
                Select all new
              </button>
              <button
                className="btn"
                type="button"
                disabled={pending || count === 0}
                onClick={() => changeSelection(() => new Set())}
              >
                Clear selection
              </button>
              <button
                ref={actionRef}
                className="btn btn--primary"
                type="button"
                disabled={pending || count === 0 || confirming}
                onClick={() => setConfirming(true)}
              >
                {count === 0
                  ? "Add selected videos"
                  : `Add ${count} selected video${count === 1 ? "" : "s"}`}
              </button>
            </div>
            <p aria-live="polite">
              {count === 0 ? "No videos selected" : `${count} selected`}
            </p>
            {confirming && (
              <div
                className="row"
                onKeyDown={(e) => {
                  if (e.key === "Escape" && !pending) closeConfirm();
                }}
              >
                <p>
                  Add {countLabel(count, "video")} from this playlist? They are
                  queued with new uploads and processed in order; this can take
                  a while.
                </p>
                <button
                  ref={confirmRef}
                  className="btn btn--primary"
                  type="button"
                  disabled={pending}
                  onClick={confirm}
                >
                  Confirm
                </button>
                <button
                  className="btn"
                  type="button"
                  disabled={pending}
                  onClick={closeConfirm}
                >
                  Cancel
                </button>
              </div>
            )}
            {pending && (
              <p className="muted" role="status">
                Adding videos…
              </p>
            )}
            {mutation.isError && (
              <p className="alert alert--danger" role="alert">
                {batchSubmitErrorMessage(mutation.error)}
              </p>
            )}
          </div>

          <div className="table-scroll">
            <table className="table catalog-table">
              <caption className="muted">
                Playlist videos, in playlist order
              </caption>
              <thead>
                <tr>
                  <th scope="col">Select</th>
                  <th scope="col">Title</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry, index) => (
                  <EntryRow
                    key={entry.video_id}
                    entry={entry}
                    index={index}
                    checked={!entry.known && selected.has(entry.video_id)}
                    disabled={pending}
                    queuedHere={queuedHere.has(entry.video_id)}
                    onToggle={() =>
                      changeSelection((current) =>
                        toggleInSet(current, entry.video_id),
                      )
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <p aria-live="polite">
        {rows === null ? "" : summarizeOutcomes(rows.map((r) => r.outcome))}
      </p>
      {rows !== null && (
        <section aria-label="Results">
          <ul>
            {rows.map((row) => (
              <li key={row.videoId}>
                <ResultText row={row} />
              </li>
            ))}
          </ul>
          {rows.some((r) => outcomeNeedsRetry(r.outcome)) && (
            <button
              className="btn"
              type="button"
              disabled={pending}
              onClick={retry}
            >
              Retry these
            </button>
          )}
        </section>
      )}
    </>
  );
}

function EntryRow({
  entry,
  index,
  checked,
  disabled,
  queuedHere,
  onToggle,
}: {
  entry: PlaylistEntry;
  index: number;
  checked: boolean;
  disabled: boolean;
  queuedHere: boolean;
  onToggle: () => void;
}) {
  const name = entryName(entry);
  const deleted = entry.processing_status === "deleted";
  const href = deleted ? null : canonicalVideoUrl(entry.video_id);
  const classes = [
    "catalog-row",
    index % 2 === 0 ? "catalog-row--even" : "catalog-row--odd",
    checked ? "catalog-row--selected" : "",
    entry.known ? "catalog-row--known" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <tr className={classes}>
      <td>
        <input
          type="checkbox"
          aria-label={`Select ${name}`}
          checked={checked}
          disabled={entry.known || disabled}
          onChange={onToggle}
        />
      </td>
      <th scope="row" className="catalog-title">
        {href === null ? (
          name
        ) : (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {name}
          </a>
        )}
      </th>
      <td>
        {entry.known
          ? knownEntryLabel(entry, queuedHere)
          : checked
            ? "New · Selected"
            : "New"}
        {deleted && (
          <>
            {" "}
            <span className="muted">
              Deleted · restore it from Recently deleted
            </span>
          </>
        )}
      </td>
    </tr>
  );
}

function ResultText({ row }: { row: ResultRow }) {
  const id = parseVideoId(row.videoId) ?? row.videoId;
  const open = (
    <>
      {" "}
      <Link to={`/videos/${id}`}>Open</Link>
    </>
  );
  switch (row.outcome) {
    case "queued":
    case "restarted":
      return (
        <>
          Queued: {id}
          {open}
        </>
      );
    case "already_submitted":
      return (
        <>
          Already added: {id}
          {open}
        </>
      );
    case "cancellation_in_progress":
      return <>{`${id}: still being deleted, try again in a minute`}</>;
    default:
      return <>{`${id}: could not be added`}</>;
  }
}
