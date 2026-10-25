"""``analyses.cache_read_tokens`` / ``cache_write_tokens`` / ``calls`` from summarizer usage (#139).

Needs Postgres.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from common.errors import LLMInvalidOutputError, LLMUnavailableError
from common.models import Segment
from tests.services.analyzer.fakes import BatchSummarizer, FakeSummarizer
from tests.services.analyzer.test_handler import (
    ROSTER,
    Rig,
    rig_factory,  # noqa: F401 - the fixture
)

pytestmark = pytest.mark.integration

# Under 900/60 chunking, 2000 one-second segments are three chunks.
THREE_CHUNKS = {"whisper": tuple(Segment(i, i + 1, f"w{i}") for i in range(2000))}

# (cache_read, cache_write, calls) per call kind: every figure distinct, so a missed,
# doubled or swapped call changes a total.
EXTRA = {"roster": (1, 20, 1), "chunk": (300, 4000, 2), "reduce": (50000, 600000, 3)}


def _expected(chunks: int) -> tuple[int, int, int]:
    return tuple(  # type: ignore[return-value]
        EXTRA["roster"][i] + chunks * EXTRA["chunk"][i] + EXTRA["reduce"][i] for i in range(3)
    )


def _usage(rig: Rig) -> tuple[int | None, int | None, int | None]:
    got = rig.latest()
    return (got.cache_read_tokens, got.cache_write_tokens, got.calls)


def _done_line(rig: Rig) -> dict[str, object]:
    (line,) = [r for r in rig.logs.records("info") if "analysis_id" in r]
    return dict(line)


def test_cache_tokens_and_calls_are_the_sum_of_roster_every_chunk_and_reduce(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    rig = rig_factory(FakeSummarizer(roster=ROSTER, extra_usage=EXTRA), segments=THREE_CHUNKS)

    rig.run()

    assert rig.summarizer.kinds.count("chunk") == 3
    assert _usage(rig) == _expected(3) == (50901, 612020, 10)
    line = _done_line(rig)
    assert (line["cache_read_tokens"], line["cache_write_tokens"], line["calls"]) == (
        50901, 612020, 10,
    )


def test_the_batch_path_stores_the_same_sums(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    summarizer = BatchSummarizer(roster=ROSTER, extra_usage=EXTRA)
    rig = rig_factory(summarizer, segments=THREE_CHUNKS)

    rig.run()

    assert summarizer.batch_calls == 1
    assert _usage(rig) == _expected(3)


def test_zero_cache_tokens_are_stored_as_zero_not_null(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    # An Ollama-like summarizer: calls are made, cache tokens are always 0.
    extra = {"roster": (0, 0, 1), "chunk": (0, 0, 1), "reduce": (0, 0, 1)}
    rig = rig_factory(FakeSummarizer(roster=ROSTER, extra_usage=extra))

    rig.run()

    assert _usage(rig) == (0, 0, 4)


def test_an_empty_transcript_stores_known_zeros_and_logs_them(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    rig = rig_factory(FakeSummarizer(extra_usage=EXTRA), segments={"whisper": ()})

    rig.run()

    assert rig.summarizer.calls == []
    assert _usage(rig) == (0, 0, 0)
    line = _done_line(rig)
    assert (line["cache_read_tokens"], line["cache_write_tokens"], line["calls"]) == (0, 0, 0)


def test_a_failed_roster_pass_keeps_its_whole_usage_in_the_totals(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    summarizer = FakeSummarizer(
        fail_on={"roster": LLMInvalidOutputError("bad json")},
        usage_on_fail=(9, 8),
        costs={"roster": 0.5, "chunk": 0.25, "reduce": 0.125},
        extra_usage=EXTRA,
    )
    rig = rig_factory(summarizer, segments=THREE_CHUNKS)

    rig.run()

    got = rig.latest()
    read, write, calls = _expected(3)
    # the fake counts one more call per priced call (roster, 3 chunks, reduce)
    assert (got.cache_read_tokens, got.cache_write_tokens, got.calls) == (read, write, calls + 5)
    # failed roster 9/8, three chunks 100/50, reduce 20/10
    assert (got.input_tokens, got.output_tokens) == (9 + 300 + 20, 8 + 150 + 10)
    # the failed roster call was priced too: 0.5 + 3 * 0.25 + 0.125
    assert got.cost_usd == 1.375


def test_a_second_job_on_a_reused_summarizer_counts_only_its_own_usage(
    rig_factory: Callable[..., Rig],  # noqa: F811
) -> None:
    summarizer = FakeSummarizer(
        roster=ROSTER, extra_usage=EXTRA, fail_on={"chunk:1": LLMUnavailableError("down")}
    )
    rig = rig_factory(summarizer, segments=THREE_CHUNKS)
    with pytest.raises(LLMUnavailableError):
        rig.run()
    # The failed chunk call left its usage pending: only the job-start take_usage()
    # discard keeps it out of the next job's totals.
    assert summarizer._cache != [0, 0] and summarizer._calls > 0
    summarizer.fail_on.clear()

    rig.run()

    assert _usage(rig) == _expected(3)
