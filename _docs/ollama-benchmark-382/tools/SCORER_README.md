# Scorer instructions (blind)

You score summaries of YouTube videos made by two unnamed systems, X and Y. You do not know, and must not try to find out, which system is which.

Files in this directory:

- `RUBRIC.md`: the rubric (faithfulness, instruction following, coverage of the main points; integers 1 to 5). Follow it exactly.
- `U01.transcript.txt` ... : the transcript text of unit U01 etc. (caption text, timestamps `[mm:ss]` at line starts). This is the only source of truth for what the video says.
- `U01.summaries.md` ... : the two summaries for that unit, `Summary X` and `Summary Y`. Each has a `FACTS (mechanical)` line (sentence count, paragraph count, markdown or preamble, JSON repairs, speakers outside roster, quotes found verbatim) that you use for the instruction-following score, then the TLDR, topics, claims and quotes. Quotes are checked mechanically: do not score them.
- A summary that reads `[NO OUTPUT: ...]` is a failed job: score it 1 for instruction following and leave faithfulness and coverage as `null`.

Procedure per unit:

1. Read the transcript. Write its 3 to 8 main points, ordered by importance, BEFORE reading either summary.
2. Read X, then Y. Score each on faithfulness, instruction following and coverage. For faithfulness, list every unsupported statement.
3. Return JSON only, one object per unit: `{"U01": {"main_points": ["..."], "X": {"faithfulness": 4, "instruction_following": 5, "coverage": 3, "unsupported": ["..."]}, "Y": {...}}, ...}`.

Do not read any other directory. Do not guess which model wrote which summary; do not mention model names. Two independent scorers get the same files (passes 1 and 2, X/Y are assigned differently in each pass).
