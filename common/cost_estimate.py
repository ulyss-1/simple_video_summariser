"""The one estimator of an LLM analysis's USD cost (issue #153; architecture.md section 10).

Cost is the transcript's character count times the median recorded
``analyses.cost_usd`` per character (``common.repo.analyses.median_cost_per_char``,
#117) of the model. It deliberately is not ``chars / 4 * price``: that ignores
the prompts, the roster and reduce passes and the output, and runs about five
times low. The recorded cost already carries the 0.5 Batches factor of the runs
that produced it, so nothing here multiplies by it again.

Pure arithmetic: no database, no adapter. The re-analysis sweep
(``services/planner/sweep.py``) and the ``/videos/{id}/analysis-estimate`` route
both call ``estimate_cost_usd`` and neither re-implements it.
"""

from __future__ import annotations

import math


def estimate_cost_usd(
    transcript_chars: int | None, cost_per_char: float | None, samples: int
) -> float | None:
    """Estimated USD to analyse a transcript, or ``None`` when unknown.

    Unknown means: no cost samples, no usable per-character figure (``None``,
    non-finite, negative or zero), or no transcript length (``None``, negative).
    A transcript of 0 characters is a valid input and costs 0.0.
    """
    if samples <= 0 or transcript_chars is None or cost_per_char is None:
        return None
    if transcript_chars < 0 or not math.isfinite(cost_per_char) or cost_per_char <= 0:
        return None
    try:
        cost = transcript_chars * cost_per_char
    except OverflowError:
        return None
    return cost if math.isfinite(cost) else None
