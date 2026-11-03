# Rubric for the #382 summary quality scoring

Each output is the stored analysis of one video: `tldr` (4 to 6 sentences, one plain paragraph, by the v1 reduce prompt), `topics`, `claims` and `quotes`. Scores are integers 1 to 5, given per output (X and Y separately) for three dimensions. Quotes are checked mechanically by the pipeline and are not scored; the scorer reads the TLDR, the topics and the claims.

## Faithfulness

Claims not supported by the transcript. The scorer checks the TLDR, the topics and every claim against the transcript text. A claim the transcript supports, including a number or a name as the captions spell it, is supported; a repeated auto-caption error (a mis-captioned company name) is not counted against the model.

- 5: every statement supported.
- 4: one minor unsupported detail that does not change the meaning (a rounded figure, an order slip).
- 3: two or three minor unsupported details, or one significant one (a wrong figure, entity, direction of a prediction, or a point attributed to the wrong person).
- 2: several significant unsupported or contradicted statements.
- 1: mostly invented, or about a different video.

The scorer lists each unsupported statement.

## Instruction following

Length and format. Mechanical facts from the pipeline are given to the scorer (sentence count, paragraph count, markdown or preamble present, repairs needed, speakers outside the roster).

- 5: TLDR is 4 to 6 sentences, one paragraph, English, plain text with no preamble or markdown; JSON valid on the first try (no repair); speakers are `unknown` or roster names; lists not padded.
- 4: one minor deviation (7 sentences, one repair).
- 3: two deviations.
- 2: three or more, or a rule broken in a way the parser caught and a repair fixed.
- 1: the output is unusable (empty, JSON in the TLDR, a list instead of a paragraph). A failed job is scored 1.

## Coverage of the main points

Before reading either summary, the scorer writes the transcript's 3 to 8 main points (ordered by importance). Coverage is the share of them the TLDR and topics carry.

- 5: at least 90%.
- 4: 70 to 89%.
- 3: 50 to 69%.
- 2: 30 to 49%.
- 1: below 30%.

Missing the single most important point caps the score at 3.

## Procedure and blindness

Two independent passes, each by a fresh LLM agent that is not the engineer and not the agent that wrote the driver. A pass gets only this rubric, the transcript text and two summaries labelled `X` and `Y` per video. The X/Y assignment is randomised per video and per pass from a recorded seed; the mapping is stored in a separate file the scorers are not given. No model name, timing or token count appears in a packet (a summary that names its own model is redacted). Scores are returned as JSON. The mapping is applied only after both passes are in.

Disagreements: for each (output, dimension), a difference of 0 or 1 between the passes is averaged (both values are kept in the data); a difference of 2 or more goes to a third fresh pass on that output alone, and the median of the three is used. The number of such cases is reported.

Returned JSON per video: `{"V01": {"main_points": [...], "X": {"faithfulness": n, "instruction_following": n, "coverage": n, "unsupported": ["..."]}, "Y": {...}}, ...}`.
