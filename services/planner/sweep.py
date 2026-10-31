"""Re-analysis sweep (issue #37; architecture.md §4, C4).

``run_reanalysis_sweep`` is one pass, called by the planner loop (#38) on each
tick. It enqueues one low-priority ``analyze`` job for every video that has a
stored transcript but no analysis at the current ``PROMPT_VERSION``, so bumping
the version re-analyses the corpus without re-downloading or re-transcribing.

Videos whose ``analyze`` job at this key is ``pending``, ``running`` or
``dead`` are skipped: ``dead`` because re-enqueueing would bypass
``max_attempts`` (the ops view, #44, is the retry path). A database error
propagates unchanged; jobs enqueued before it stay, and the next run skips them.

Spend cap (#99). With ``SUMMARIZER=anthropic`` the sweep also stops at
``SWEEP_MAX_JOBS_ANTHROPIC`` jobs *in total per prompt version*: the number of
``analyze`` jobs already enqueued at this key and ``REANALYSIS_PRIORITY``, in any
state, is read from ``jobs`` on every run, so the cap survives a planner restart.
A run enqueues at most ``min(limit, cap - already_enqueued)``. Jobs enqueued by
the transcribe handler or interactively use other priorities and are not counted.
``plan_reanalysis_sweep`` is the one computation behind both the live run and the
read-only ``--sweep-dry-run`` report, so they cannot disagree.

USD budget (#153). With ``SUMMARIZER=anthropic`` the sweep also stops before the
estimated cumulative spend for this prompt version would exceed
``SWEEP_MAX_USD_ANTHROPIC`` (whichever of the two caps binds first; ``cap_reason``
is ``jobs`` or ``usd``). Spent-or-committed USD is summed over this sweep's jobs
(``analyze``, this dedupe key, ``REANALYSIS_PRIORITY``): the recorded
``analyses.cost_usd`` of a finished one (the oldest analysis of its video at the
version, which is the sweep's own because the sweep only picks videos with none;
a later interactive Re-analyse is not sweep spend), else its estimate, plus the
``analyze_failed_usage`` rows (#252) of those jobs; a NULL cost counts as the
estimate, and a failed row whose ``job_id`` is NULL (its job was deleted) is not
counted. A candidate is estimated by ``common.cost_estimate.estimate_cost_usd``:
its transcript characters times the median recorded cost per character of the
model (``median_cost_per_char``, #117), deliberately not ``chars / 4 x price``,
which runs about 5x low because it ignores prompts, roster and reduce passes and
the output. The walk follows the candidate order and stops at the first one that
would not fit; it never skips to a cheaper one. The recorded cost already carries
the batch factor, and after flipping ``ANTHROPIC_BATCH`` the median follows only
as new analyses replace the newest 20. Cold start: with no cost samples the
estimate is unknown and at most ``SWEEP_PROBE_JOBS`` sweep jobs in total are
enqueued for the version (counting those already there) until a cost is
recorded. Budget ``0`` enqueues nothing. The budget is dormant under
``SUMMARIZER=ollama`` or ``free``: it is not read and no cost query runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from common.config import Settings
from common.cost_estimate import estimate_cost_usd
from common.queue import PRIORITY_REANALYSIS, JobQueue, analyze_dedupe_key
from common.repo.analyses import median_cost_per_char
from common.repo.transcripts import analysis_transcript_chars

_logger = logging.getLogger(__name__)

#: Below new videos (0) and interactive submissions (+10), so a sweep never starves them.
REANALYSIS_PRIORITY = PRIORITY_REANALYSIS

#: The cap's default, read from the setting so the two cannot drift apart.
DEFAULT_MAX_JOBS_ANTHROPIC = int(Settings.model_fields["SWEEP_MAX_JOBS_ANTHROPIC"].default)

#: The USD budget's default and the model the estimate is for, from the settings.
DEFAULT_MAX_USD_ANTHROPIC = float(Settings.model_fields["SWEEP_MAX_USD_ANTHROPIC"].default)
DEFAULT_MODEL = str(Settings.model_fields["ANTHROPIC_MODEL"].default)

#: Cold start: with no recorded cost, at most this many sweep jobs in total per version.
SWEEP_PROBE_JOBS = 5

#: Slack when comparing float dollars, so a candidate estimated exactly at the
#: remaining budget (0.05 as 25,000 x 2e-6) is not refused by rounding.
_USD_EPSILON = 1e-9

_ELIGIBLE_SQL = r"""
    FROM videos v
    WHERE EXISTS (
            SELECT 1 FROM transcripts t
            WHERE t.video_id = v.video_id AND t.full_text ~ '\S')
      AND NOT EXISTS (
            SELECT 1 FROM analyses a
            WHERE a.video_id = v.video_id AND a.prompt_version = %(prompt_version)s)
      AND NOT EXISTS (
            SELECT 1 FROM jobs j
            WHERE j.video_id = v.video_id AND j.kind = 'analyze'
              AND j.dedupe_key = %(dedupe_key)s
              AND j.state IN ('pending', 'running', 'dead'))
"""

_CANDIDATES_SQL = (
    "SELECT v.video_id "
    + _ELIGIBLE_SQL
    + " ORDER BY v.published_at DESC NULLS LAST, v.video_id LIMIT %(limit)s"
)
_COUNT_SQL = "SELECT count(*) " + _ELIGIBLE_SQL

_ALREADY_ENQUEUED_SQL = """
    SELECT count(*) FROM jobs
    WHERE kind = 'analyze' AND dedupe_key = %(dedupe_key)s AND priority = %(priority)s
"""

_SWEEP_JOBS_SQL = """
    SELECT j.video_id, j.state,
           (SELECT a.cost_usd::float8 FROM analyses a
             WHERE a.video_id = j.video_id AND a.prompt_version = %(prompt_version)s
             ORDER BY a.created_at, a.id LIMIT 1)
    FROM jobs j
    WHERE j.kind = 'analyze' AND j.dedupe_key = %(dedupe_key)s AND j.priority = %(priority)s
      AND j.state IN ('pending', 'running', 'done')
"""

# A failed row whose job_id is NULL has no job to match and is not counted.
_FAILED_USAGE_SQL = """
    SELECT u.video_id, u.cost_usd::float8
    FROM analyze_failed_usage u JOIN jobs j ON j.id = u.job_id
    WHERE j.kind = 'analyze' AND j.dedupe_key = %(dedupe_key)s AND j.priority = %(priority)s
"""


@dataclass(frozen=True)
class SweepPlan:
    """What a sweep pass would do right now (#99)."""

    prompt_version: str
    summarizer: str
    candidates: int  #: every eligible video, not truncated at ``limit``
    already_enqueued: int
    cap: int | None  #: ``None`` when the summarizer is not capped
    limit: int
    would_enqueue_this_run: int
    would_remain_after_cap: int  #: candidates the cap leaves unanalysed
    cap_stopped: bool  #: the cap, not ``limit`` or running out, ends this run
    video_ids: tuple[str, ...]  #: the videos a live run would enqueue
    cap_reason: str | None = None  #: ``jobs`` or ``usd``, whichever ended the run; ``None`` if neither
    budget_usd: float | None = None  #: ``None`` when the summarizer is not anthropic
    est_usd_committed: float | None = None  #: spent or committed so far; ``None`` = unknown
    est_usd_this_run: float | None = None  #: this run's candidates; ``None`` = unknown
    cost_per_char_samples: int | None = None  #: ``None`` when no cost query ran


def _committed_usd(
    conn: psycopg.Connection[Any],
    *,
    prompt_version: str,
    dedupe_key: str,
    cost_per_char: float,
    samples: int,
) -> float:
    """Spent-or-committed USD of this sweep's jobs and their failed attempts (#153)."""
    params = {
        "prompt_version": prompt_version,
        "dedupe_key": dedupe_key,
        "priority": REANALYSIS_PRIORITY,
    }
    with conn.cursor() as cur:
        cur.execute(_SWEEP_JOBS_SQL, params)
        jobs = [
            (str(r[0]), str(r[1]), None if r[2] is None else float(r[2])) for r in cur.fetchall()
        ]
        cur.execute(_FAILED_USAGE_SQL, params)
        failed = [(str(r[0]), None if r[1] is None else float(r[1])) for r in cur.fetchall()]
    # A finished job's recorded cost wins; everything without a figure is estimated.
    needs_estimate = [vid for vid, state, rec in jobs if state != "done" or rec is None]
    needs_estimate += [vid for vid, cost in failed if cost is None]
    chars = analysis_transcript_chars(conn, sorted(set(needs_estimate)))

    def estimate(video_id: str) -> float:
        return estimate_cost_usd(chars.get(video_id), cost_per_char, samples) or 0.0

    total = 0.0
    for vid, state, recorded in jobs:
        total += recorded if state == "done" and recorded is not None else estimate(vid)
    for vid, cost in failed:
        total += cost if cost is not None else estimate(vid)
    return total


def _walk_budget(
    conn: psycopg.Connection[Any],
    video_ids: tuple[str, ...],
    *,
    budget: float,
    committed: float | None,
    per_char: float | None,
    samples: int,
    probe_room: int,
) -> tuple[tuple[str, ...], float | None, bool]:
    """Keep the leading candidates that fit the budget: ``(kept, their USD or None, stopped)``.

    Stops at the first one that does not fit and never skips to a cheaper one.
    With no cost figure (cold start) the estimate is unknown and at most
    ``probe_room`` candidates pass.
    """
    if budget <= 0:
        return (), (0.0 if per_char is not None else None), bool(video_ids)
    if per_char is None or committed is None:
        kept = video_ids[:probe_room]
        return kept, None, len(kept) < len(video_ids)
    chars = analysis_transcript_chars(conn, video_ids)
    spent = committed
    this_run = 0.0
    kept_ids: list[str] = []
    for video_id in video_ids:
        cost = estimate_cost_usd(chars.get(video_id), per_char, samples)
        if cost is None or spent + cost > budget + _USD_EPSILON:
            break
        spent += cost
        this_run += cost
        kept_ids.append(video_id)
    return tuple(kept_ids), this_run, len(kept_ids) < len(video_ids)


def plan_reanalysis_sweep(
    conn: psycopg.Connection[Any],
    *,
    prompt_version: str,
    summarizer_name: str,
    limit: int = 500,
    max_jobs_anthropic: int = DEFAULT_MAX_JOBS_ANTHROPIC,
    max_usd_anthropic: float = DEFAULT_MAX_USD_ANTHROPIC,
    model: str = DEFAULT_MODEL,
    probe_jobs: int = SWEEP_PROBE_JOBS,
    median: tuple[float | None, int] | None = None,
) -> SweepPlan:
    """Read the database and work out one pass. Writes nothing; ends its read transaction.

    ``median`` is ``(cost per character, samples)`` of ``model``; ``None`` reads it
    from the database (anthropic only). Tests pass fake figures.
    """
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")
    dedupe_key = analyze_dedupe_key(prompt_version, summarizer_name)
    anthropic = summarizer_name == "anthropic"
    cap = max_jobs_anthropic if anthropic else None
    params = {"prompt_version": prompt_version, "dedupe_key": dedupe_key}
    committed: float | None = None
    this_run: float | None = None
    samples = 0
    usd_stopped = False
    with conn.cursor() as cur:
        cur.execute(
            _ALREADY_ENQUEUED_SQL, {"dedupe_key": dedupe_key, "priority": REANALYSIS_PRIORITY}
        )
        row = cur.fetchone()
        already = int(row[0]) if row is not None else 0
        cur.execute(_COUNT_SQL, params)
        row = cur.fetchone()
        total = int(row[0]) if row is not None else 0
        room = None if cap is None else max(0, cap - already)
        take = min(limit, total, total if room is None else room)
        video_ids: tuple[str, ...] = ()
        if take > 0:
            cur.execute(_CANDIDATES_SQL, {**params, "limit": take})
            video_ids = tuple(str(r[0]) for r in cur.fetchall())
    if anthropic:
        per_char, samples = median if median is not None else median_cost_per_char(conn, model)
        if per_char is not None and samples <= 0:
            per_char = None
        if per_char is not None:
            committed = _committed_usd(
                conn,
                prompt_version=prompt_version,
                dedupe_key=dedupe_key,
                cost_per_char=per_char,
                samples=samples,
            )
        video_ids, this_run, usd_stopped = _walk_budget(
            conn,
            video_ids,
            budget=max_usd_anthropic,
            committed=committed,
            per_char=per_char,
            samples=samples,
            probe_room=max(0, probe_jobs - already),
        )
    # End the read transaction: enqueue() must open its own, not a savepoint inside
    # this one that the planner's closing connection would roll back (#199; same
    # reason as services/planner/fallback.py, #92).
    conn.commit()
    jobs_stopped = room is not None and room < min(limit, total)
    cap_reason = "usd" if usd_stopped else ("jobs" if jobs_stopped else None)
    if usd_stopped:
        remain = total - len(video_ids)
    else:
        remain = 0 if room is None else max(0, total - room)
    return SweepPlan(
        prompt_version=prompt_version,
        summarizer=summarizer_name,
        candidates=total,
        already_enqueued=already,
        cap=cap,
        limit=limit,
        would_enqueue_this_run=len(video_ids),
        would_remain_after_cap=remain,
        cap_stopped=cap_reason is not None,
        video_ids=video_ids,
        cap_reason=cap_reason,
        budget_usd=max_usd_anthropic if anthropic else None,
        est_usd_committed=committed,
        est_usd_this_run=this_run,
        cost_per_char_samples=samples if anthropic else None,
    )


def run_reanalysis_sweep(
    conn: psycopg.Connection[Any],
    queue: JobQueue,
    *,
    prompt_version: str,
    summarizer_name: str,
    limit: int = 500,
    max_jobs_anthropic: int = DEFAULT_MAX_JOBS_ANTHROPIC,
    max_usd_anthropic: float = DEFAULT_MAX_USD_ANTHROPIC,
    model: str = DEFAULT_MODEL,
    probe_jobs: int = SWEEP_PROBE_JOBS,
    median: tuple[float | None, int] | None = None,
) -> int:
    """Enqueue up to ``limit`` re-analysis jobs (within both caps) and return how many."""
    plan = plan_reanalysis_sweep(
        conn,
        prompt_version=prompt_version,
        summarizer_name=summarizer_name,
        limit=limit,
        max_jobs_anthropic=max_jobs_anthropic,
        max_usd_anthropic=max_usd_anthropic,
        model=model,
        probe_jobs=probe_jobs,
        median=median,
    )
    dedupe_key = analyze_dedupe_key(prompt_version, summarizer_name)

    enqueued = 0
    for video_id in plan.video_ids:
        job_id = queue.enqueue(
            "analyze", video_id, dedupe_key=dedupe_key, priority=REANALYSIS_PRIORITY
        )
        if job_id is not None:
            enqueued += 1

    if plan.cap_stopped:
        committed_now = (
            None
            if plan.est_usd_committed is None
            else plan.est_usd_committed + (plan.est_usd_this_run or 0.0)
        )
        _logger.warning(
            "planner.reanalysis_sweep.cap_reached",
            extra={
                "prompt_version": prompt_version,
                "summarizer": summarizer_name,
                "reason": plan.cap_reason,
                "cap": plan.cap,
                "budget_usd": plan.budget_usd,
                "committed_usd": committed_now,
                "already_enqueued": plan.already_enqueued,
                "unenqueued": plan.candidates - len(plan.video_ids),
            },
        )
    _logger.info(
        "planner.reanalysis_sweep",
        extra={
            "prompt_version": prompt_version,
            "summarizer": summarizer_name,
            "enqueued": enqueued,
            "limit": limit,
            "limit_reached": len(plan.video_ids) >= limit,
            "cap": plan.cap,
            "already_enqueued": plan.already_enqueued,
        },
    )
    return enqueued
