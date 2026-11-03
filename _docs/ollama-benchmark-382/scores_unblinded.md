# Blind quality scores, un-blinded by the orchestrator (2 independent LLM scorer passes, 1-5 rubric in RUBRIC.md)

Scorers: two fresh LLM agents (same model family as the engineer), blind to the model (labels X/Y randomised per unit), no access to the mapping. Mean absolute difference between passes 0.19 (n=32); no summary pair differed by 2 or more, so no third pass was needed.

## Per unit, mean of the two passes (F = faithfulness, I = instruction following, C = coverage; '-' = run produced no output, so not scorable)

| Unit | Video | Setting | gemma4:e4b F/I/C | qwen3.5:4b F/I/C |
|---|---|---|---|---|
| U01 | bIvyxp-FAGU | main | 5.0/5.0/4.0 | 2.0/4.0/3.0 |
| U02 | qovnfjVd8cs | main | 4.0/5.0/4.0 | -/1.0/- |
| U03 | M8_DnpYV2hY | main | 5.0/5.0/4.0 | 3.5/5.0/4.0 |
| U04 | EfGtEDJyRvk | main | 4.0/3.0/4.0 | -/1.0/- |
| U05 | EfGtEDJyRvk | ctx16k | 4.5/3.5/4.0 | 2.0/2.0/3.5 |
| U06 | eEE3S-MvzEs | ctx16k | 3.0/3.5/4.0 | 3.0/2.5/5.0 |
