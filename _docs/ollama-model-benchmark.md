# Ollama model benchmark on the deploy host

Measured on 2026-11-02 for issue #149. The decision is not made: see
"Status" at the end. A larger run on real transcripts is issue #382.

## Host and method

- Host: HP ProBook 640 G1, i7-4712MQ (4C/8T, AVX2), 14 GB RAM, SATA3 SSD,
  CPU only. Ollama listens on `172.17.0.1:11434`
  (`_docs/deploy-host.md` section 8).
- Prompt, identical for every model:
  `Summarize in 3 sentences: $(head -c 4000 /etc/services)`, run as
  `ollama run <model> --verbose --think=false`. Run by the owner on the host.
- One run per model. The prompt is not a video transcript. Treat the numbers
  as indicative, not as a bake-off.

## Results (measured)

| Model | Size on disk | Prompt tokens | Prefill (cold) | Generation | Load | Total (cold) | Output tokens |
|---|---|---|---|---|---|---|---|
| `qwen3.5:2b` | 2.7 GB | 1699 | 46.8 tok/s | 8.58 tok/s | 6.9 s | 1 m 07 s | 207 |
| `qwen3.5:4b` | 3.3 GB | 1697 | 24.7 tok/s | 5.63 tok/s | 14 s | about 1 m 30 s (estimated) | 123 |
| `gemma4:e4b` | 6.6 GB | 1637 | 22.4 tok/s | 5.76 tok/s | 27.5 s | 1 m 59 s | 106 |
| `nemotron-3-nano:4b` | 2.8 GB | 1844 | 17.2 tok/s | 5.63 tok/s | 5.8 s | 2 m 15 s | 123 |
| `qwen3.5:9b` | 6.6 GB | 1699 | 13.7 tok/s | 3.31 tok/s | 27.9 s | 3 m 02 s | 101 |

`gemma4:26b` (18 GB) is installed on the host but was not benchmarked. It does
not fit in 14 GB of RAM.

Notes on the `qwen3.5:4b` row:

- The first run left thinking on: 2302 output tokens, 8 m 57 s, prefill
  24.7 tok/s, generation 5.06 tok/s. Prefill is valid; the total is not
  representative, because about 2190 of the tokens were reasoning.
- The `--think=false` rerun reported `prompt eval cached: 1693`, so its prefill
  (10.47 tok/s) and its 22 s total are invalid. Its generation rate
  (5.63 tok/s) is valid.
- The cold total is the cold prefill (1697 / 24.7 = 69 s) plus the measured
  generation (22 s). Rerun after `ollama stop qwen3.5:4b` to measure it.
- Prompt token counts differ because each model has its own tokenizer, so
  prefill rates are not strictly like for like.

## How to measure cleanly

- Unload the model first: `OLLAMA_HOST=172.17.0.1:11434 ollama stop <model>`.
  Otherwise the prompt cache hides the prefill cost.
- Check that `prompt eval cached` is absent or 0 in the `--verbose` output.
- Always pass `--think=false`. The analyzer adapter sends `"think": false`
  (`adapters/summarize/ollama.py`), so production never pays for reasoning.
- Keep the prompt below `OLLAMA_NUM_CTX`, or Ollama truncates it silently.

## Prefill time for a 12 000-token transcript (estimated)

About one hour of speech. Extrapolated linearly from the rates above; a longer
prompt may run slower.

| Model | Prefill estimate |
|---|---|
| `qwen3.5:2b` | about 4.3 min |
| `qwen3.5:4b` | about 8.1 min |
| `gemma4:e4b` | about 8.9 min |
| `nemotron-3-nano:4b` | about 11.7 min |
| `qwen3.5:9b` | about 14.6 min |

## Quality, checked against the first 4000 bytes of `/etc/services`

One sample, not an evaluation.

| Model | Followed "3 sentences" | Faithful to the source | Defects |
|---|---|---|---|
| `qwen3.5:2b` | No: 3 paragraphs, 8 sentences | Mostly | Unsupported claims: protocols "deprecated due to security risks", "IPv6 support is notably absent" (the file has dhcpv6 entries). Calls FTP and Telnet "Unix utilities". HTTPS over TCP and UDP and IPX are correct (both are in the file). |
| `qwen3.5:4b` | Yes | Yes | None found. Names syslog and DNS, both present. |
| `gemma4:e4b` | Yes | Yes | None found. Ports 80, 22, 53 (tcp/udp), 25 and 110 are correct. Most specific of the five. |
| `nemotron-3-nano:4b` | Yes | Mostly | Names the tool "NMA package" instead of `nmap`. Kerberos 88 and NTP 123 are correct. |
| `qwen3.5:9b` | Yes | Yes | None found. Names `nmap` correctly. Less specific than `gemma4:e4b` (no port numbers). |

## Reading

- `qwen3.5:2b` is about 1.9x faster at prefill and 1.5x at generation than
  `qwen3.5:4b`, but it broke the length instruction and added claims the source
  does not support. That is a risk for a summarizer. Not a candidate.
- `nemotron-3-nano:4b` has the slowest prefill of the 4B class and one factual
  slip. Not a candidate.
- `qwen3.5:9b` is the slowest (1.8x slower prefill and 1.7x slower generation
  than 4b) with no visible quality gain on this sample. Not a candidate for
  this CPU.
- Two candidates remain: `qwen3.5:4b` and `gemma4:e4b`.

## The two candidates

| | `qwen3.5:4b` | `gemma4:e4b` |
|---|---|---|
| Prefill (cold) | 24.7 tok/s | 22.4 tok/s (about 10% slower) |
| Generation | 5.63 tok/s | 5.76 tok/s |
| Load | 14 s | 27.5 s |
| Size | 3.3 GB | 6.6 GB |

`qwen3.5:4b`:

- Pros: half the memory, which matters on a 14 GB host that also runs Whisper
  large-v3, Postgres and the stack (headroom is an estimate, not measured);
  faster start and about 10% faster prefill; it is the current default, so the
  prompts, adapter and tests were built against it; faithful output that
  followed the length rule.
- Cons: less specific in this sample (no port numbers); one sample only.

`gemma4:e4b`:

- Pros: the most specific and accurate summary of the five; generation speed
  equal to the 4b.
- Cons: twice the memory, which is tightest when Whisper and Ollama run at the
  same time; the 27.5 s load is paid again whenever Ollama unloads the model
  (default after 5 minutes idle); about 10% slower prefill, growing with
  transcript length; a new model means re-checking prompts and output parsing;
  its licence terms were not checked.

## Real-transcript benchmark (issue #382)

Measured on 2026-11-03, host time (IST), 02:38 to 08:54, on the same host as above. This section is evidence for the owner's decision on `OLLAMA_MODEL`; it changes no default. Raw per-call data, the run tables, the frozen-transcript hashes, the rubric, the un-blinded scores and the `ytdigest eval report` output are in `_docs/ollama-benchmark-382/`. No transcript text and no prompt is committed.

### Method

- Both models ran through the real adapter (`OllamaSummarizer`, via the analyzer's `make_analyze_handler` and `services.eval.runner.evaluate_video`), with the live analyzer's settings: `OLLAMA_NUM_CTX` 8192 (the code default; the live stack does not set it), `OLLAMA_OUTPUT_RESERVE_TOKENS` 2048, `CHUNK_SEC` 900, `OVERLAP_SEC` 60, `OLLAMA_TIMEOUT_SEC` 900, `PROMPT_VERSION` v1, `think: false` (sent by the adapter). The adapter was not edited. A scratch driver wrapped its transport to keep Ollama's raw timing fields of every HTTP call.
- Ollama 0.40.13 at `172.17.0.1:11434` (the `ollama` CLI defaults to `127.0.0.1`, an empty server, so `OLLAMA_HOST=172.17.0.1:11434` was set for every CLI call). Models `qwen3.5:4b` d8b0f5e9760c and `gemma4:e4b` dc35e8d9c606. The Ollama service environment sets only `OLLAMA_HOST`. Repo commit `fa92b8c`.
- Every run was cold: both models were unloaded before each run and `/api/ps` was empty. Model order per video was randomised from seed 382. Transcripts are YouTube captions (manual for `M8_DnpYV2hY`, auto for the rest), frozen to files whose sha256 is in `frozen_transcripts.json`, and loaded into a throwaway Postgres.
- The live transcriber and analyzer containers were stopped for the runs; the rest of the stack, the owner's desktop apps (browser, editor) and the Claude CLI were running. A sidecar sampled pressure stall (PSI), swap activity and `MemAvailable` every 5 s from 03:30. Peak memory columns below come from the driver's 2 s samples. The swap peak includes about 3.3 GB that other processes had already pushed to swap before each run.
- A call is capped at 900 s, a run at 45 minutes, and a model stops after 3 failures. The memory watchdog (1 GB `MemAvailable`) never tripped.
- A labelled extra experiment, `ctx16k`, re-ran the two long videos with `OLLAMA_NUM_CTX=16384` and everything else identical (75 minute cap, 3-failure rule not applied). It is never mixed into the main tables.

### Videos actually measured: 5 of the 10 planned

YouTube refused the subtitle fetch (HTTP 429) for five of the ten confirmed videos in two passes with backoff: `PxP7IoagbtA`, `_Yt7NuD-nbk`, `_m1XcP1addc`, `hoYWNqh_u5I` and `HKskNSEb5_c`. No substitute was found in the time available (the issue names reserves only for two channels). Phase 2 therefore started with the five frozen videos, so not all transcripts were frozen before the first model call. The length spread requirement is still met (two videos at 3.5k to 4.5k estimated tokens, one above 10k).

| Video         | Channel                   | Duration | Captions       | Chars | Est. tokens (repo estimator) | Chunks |
| ------------- | ------------------------- | -------- | -------------- | ----- | ---------------------------- | ------ |
| `bIvyxp-FAGU` | Dr. Eric Berg DC          | 10:59    | auto `en`      | 10622 | 3541                         | 1      |
| `qovnfjVd8cs` | Dr. Eric Berg DC          | 12:34    | auto `en`      | 11740 | 3914                         | 1      |
| `M8_DnpYV2hY` | BWB - Business With Brian | 12:26    | manual `en-US` | 12774 | 4258                         | 1      |
| `EfGtEDJyRvk` | Joseph Carlson            | 21:40    | auto `en`      | 23366 | 7789                         | 2      |
| `eEE3S-MvzEs` | Joseph Carlson            | 46:48    | auto `en`      | 48558 | 16186                        | 4      |

The model tokenizers count about the same as each other (3940 against 3862 prompt tokens for the roster call of `bIvyxp-FAGU`). Per-call prompt tokens are in the tables below.

### Main protocol (`OLLAMA_NUM_CTX` 8192)

Columns: calls are roster / chunk / reduce / repair; rates are sums of counts over sums of durations of the successful calls; "length" counts calls that ended with `done_reason` `length`. Wall times are the driver's, including model load. The full table with memory and PSI columns is `_docs/ollama-benchmark-382/tables.md`.

| Video         | Model      | Result               | Wall   | Calls   | Prefill tok/s | Gen tok/s | Load   | Prompt tokens per call             | Output tokens per call           |
| ------------- | ---------- | -------------------- | ------ | ------- | ------------- | --------- | ------ | ---------------------------------- | -------------------------------- |
| `bIvyxp-FAGU` | gemma4:e4b | ok                   | 878 s  | 1/1/1/0 | 20.4          | 6.4       | 20.7 s | 3940, 4820, 2257                   | 20, 1872, 137                    |
| `bIvyxp-FAGU` | qwen3.5:4b | ok                   | 904 s  | 1/1/1/0 | 21.7          | 4.2       | 10.9 s | 3862, 4766, 1873                   | 21, 1488, 218                    |
| `qovnfjVd8cs` | gemma4:e4b | ok                   | 951 s  | 1/1/1/0 | 21.1          | 6.5       | 20.5 s | 3740, 5109, 2677                   | 22, 2289, 171                    |
| `qovnfjVd8cs` | qwen3.5:4b | failed               | 1084 s | 1/1/0/0 | 21.2          | 4.5       | 5.1 s  | 3682                               | 21                               |
| `M8_DnpYV2hY` | gemma4:e4b | ok                   | 830 s  | 1/1/1/0 | 21.6          | 7.2       | 21.2 s | 3530, 4361, 2517                   | 75, 2127, 156                    |
| `M8_DnpYV2hY` | qwen3.5:4b | ok                   | 938 s  | 1/1/1/0 | 23.5          | 4.5       | 10.9 s | 3491, 4319, 2334                   | 45, 1937, 223                    |
| `EfGtEDJyRvk` | gemma4:e4b | ok                   | 1590 s | 1/3/1/0 | 21.1          | 7.1       | 19.5 s | 3495, 5653, 1737, 4005, 4692       | 96, 1675, 989, 1597, 179         |
| `EfGtEDJyRvk` | qwen3.5:4b | failed               | 1050 s | 1/1/0/0 | 23.3          | 4.0       | 11.2 s | 3421, 5593                         | 19, 2599                         |
| `eEE3S-MvzEs` | gemma4:e4b | failed               | 1547 s | 1/3/0/0 | 21.6          | 6.8       | 20.0 s | 3532, 5746, 1363, 6029             | 18, 2093, 851, 2163              |
| `eEE3S-MvzEs` | qwen3.5:4b | timeout (45 min cap) | 2698 s | 1/5/0/0 | 23.9          | 4.4       | 11.0 s | 3429, 5665, 1356, 5958, 1855, 6010 | 19, 1767, 1051, 1396, 1261, 1172 |

Rates and cost, over the two videos both models completed (`bIvyxp-FAGU`, `M8_DnpYV2hY`):

| Metric                                 | qwen3.5:4b mean (worst) | gemma4:e4b mean (worst) |
| -------------------------------------- | ----------------------- | ----------------------- |
| Prefill                                | 22.6 tok/s (21.7)       | 21.0 tok/s (20.4)       |
| Generation                             | 4.4 tok/s (4.2)         | 6.8 tok/s (6.4)         |
| Model load, cold                       | 10.9 s (10.9)           | 21.0 s (21.2)           |
| Wall per run                           | 921 s (938)             | 854 s (878)             |
| Seconds of compute per minute of video | 78.9 (82.3)             | 73.3 (79.9)             |

On the long videos the compute cost was 62 to 75 seconds per minute of video for both models when a run succeeded. One repeat of `qovnfjVd8cs` with gemma4:e4b took 884 s against 951 s the first time (7% apart), which is the only run-to-run noise measured. Prefill matches the short-prompt numbers above (about 20 to 24 tok/s). Gemma generates about 1.5 times faster than qwen here, which differs from the short-prompt result (5.76 against 5.63 tok/s); with one run per cell this is indicative only.

Cache check: Ollama 0.40.13 returns no cached-token field in `/api/chat`. No call fell below the "suspected cache hit" threshold (50% of the estimated request tokens divided by 1.5), and the first call of every run is cold by construction.

### Failures and their causes

| Run                         | Result                                    | Cause                                                                                                                                                                                                                                                                                                                                                                       |
| --------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| qwen3.5:4b on `qovnfjVd8cs` | failed after 1084 s                       | The second call (`analyze_chunk`) hit the 900 s call timeout (`TimeoutError`). No timing data exists for it. The host was under memory pressure at the time, but the sampler was not running yet, so the cause is undetermined: memory pressure and a runaway generation are both possible (a 4 tok/s generation runs past 900 s long before it reaches the context limit). |
| qwen3.5:4b on `EfGtEDJyRvk` | failed after 1050 s                       | Runaway generation: the `analyze_chunk` call produced 2599 tokens after a 5593 token prompt and stopped at the 8192 context limit (`done_reason` `length`, adapter error "ran out of context"). The sidecar shows max PSI full10 0.14, minimum `MemAvailable` 5.8 GB and no swap-out, so this is not memory.                                                                |
| gemma4:e4b on `eEE3S-MvzEs` | failed after 1547 s                       | The same mode: the fourth call had a 6029 token prompt and produced 2163 tokens, then stopped at the context limit.                                                                                                                                                                                                                                                         |
| qwen3.5:4b on `eEE3S-MvzEs` | stopped at the 45 minute run cap (2698 s) | Six calls done, each chunk call 1.0k to 1.8k output tokens at about 4.4 tok/s. The video has 4 chunks; the same model needed 2918 s for it at 16384, so a run of about 45 minutes or more is the expected cost of a 47 minute video at this generation speed.                                                                                                                                                                    |

Both failures at the context limit are the same arithmetic: a chunk prompt of 5.6k to 6.0k tokens leaves 8192 minus the prompt, about 2.2k to 2.6k tokens, and the adapter's output reserve is 2048. A chunk reply of 2000 to 2600 tokens is common for both models, so a prompt near the 6144 token budget plus a long reply does not fit. Output tokens of chunk calls at 8192:

| Model      | Chunk calls | Mean output tokens | Max output tokens | Calls over 2048 | Calls ended by `length` |
| ---------- | ----------- | ------------------ | ----------------- | --------------- | ----------------------- |
| qwen3.5:4b | 8           | 1584               | 2599              | 1               | 1                       |
| gemma4:e4b | 9           | 1740               | 2289              | 4               | 1                       |

Of 10 main runs, 4 failed or were cut at 8192 (qwen 3 of 5, gemma 1 of 5). This looks like a settings effect (`OLLAMA_NUM_CTX` 8192 with `CHUNK_SEC` 900), not a model effect: see the experiment below.

### Labelled experiment: `OLLAMA_NUM_CTX` 16384 (`ctx16k`)

Everything else identical. Run on the two long videos only.

| Video         | Model      | Result | Wall   | Prefill tok/s | Gen tok/s | Prompt tokens per call             | Output tokens per call          | Length | Peak used | Min `MemAvailable` |
| ------------- | ---------- | ------ | ------ | ------------- | --------- | ---------------------------------- | ------------------------------- | ------ | --------- | ------------------ |
| `EfGtEDJyRvk` | gemma4:e4b | ok     | 1454 s | 21.4          | 6.7       | 3495, 6644, 3970, 4144             | 18, 2329, 1375, 188             | 0      | 12.8 GB   | 1.7 GB             |
| `EfGtEDJyRvk` | qwen3.5:4b | ok     | 1644 s | 23.2          | 4.2       | 3421, 6572, 3914, 3861             | 48, 1564, 1861, 169             | 0      | 9.8 GB    | 4.8 GB             |
| `eEE3S-MvzEs` | gemma4:e4b | ok     | 2932 s | 20.7          | 6.3       | 3532, 6440, 7228, 7523, 1889, 8086 | 18, 2126, 2144, 2069, 1184, 166 | 0      | 13.1 GB   | 1.4 GB             |
| `eEE3S-MvzEs` | qwen3.5:4b | ok     | 2918 s | 22.7          | 4.0       | 3429, 6353, 7143, 7414, 1878, 6157 | 19, 1738, 2116, 824, 967, 254   | 0      | 10.5 GB   | 4.0 GB             |

All four `ctx16k` runs succeeded, including the two that failed at 8192 (qwen on `EfGtEDJyRvk`; gemma on `eEE3S-MvzEs`) and the one that hit the cap (qwen on `eEE3S-MvzEs`, which finished in 2918 s here). Note that at 16384 the adapter's chunk budget also grows (about 14 000 tokens), so the chunk calls ran with larger prompts: the number of calls differs from the 8192 runs (two chunk calls for `EfGtEDJyRvk` instead of three, four for `eEE3S-MvzEs`). Results at the two settings are therefore not run-for-run comparable. The cost is memory: the model's KV cache for the larger window is part of the figures above (gemma runner 7.9 to 8.2 GB, qwen 5.0 to 5.8 GB, against 7.6 and 4.1 to 4.4 GB at 8192 for the shorter videos).

### Memory without a concurrent transcription

Measured with the live transcriber stopped but the owner's desktop apps and the rest of the stack running. The concurrent step with Whisper was not done (see "Not done").

|                                                     | qwen3.5:4b                        | gemma4:e4b                                     |
| --------------------------------------------------- | --------------------------------- | ---------------------------------------------- |
| Peak used memory (total minus `MemAvailable`), 8192 | 8.8 to 10.5 GB                    | 12.3 to 12.9 GB                                |
| Peak used memory, 16384                             | 9.8 to 10.5 GB                    | 12.8 to 13.1 GB                                |
| Ollama runner RSS peak                              | 4.1 to 5.8 GB (5.0 to 5.8 at 16k) | 7.6 to 8.2 GB                                  |
| Minimum `MemAvailable`                              | 4.0 to 5.8 GB                     | 1.4 to 2.3 GB                                  |
| Swap in use at the peak                             | 3.4 to 3.7 GB                     | 3.3 to 7.0 GB                                  |
| Swap written during a run (sidecar), worst          | 532 MB                            | 3.9 GB (`EfGtEDJyRvk`), 3.7 GB (`eEE3S-MvzEs`) |

Gemma's runner is about 2 GB larger than qwen's at the same settings and it leaves 1.4 to 2.3 GB free on this 14 GB host with Whisper stopped. The memory watchdog (1 GB) never tripped, but the margin is thin.

### Blind quality scores

Method: two independent scorer passes, each by a fresh LLM agent of the same model family as the engineer (so this is an LLM-judge result, not human review). The scorers saw only the rubric (`_docs/ollama-benchmark-382/RUBRIC.md`, published on #382 before the first model call), the transcript text and two summaries labelled X and Y per unit, with X and Y randomised per unit and per pass; they never saw the model, timings or mapping. The mapping was applied after both passes. Mean absolute difference between the passes 0.19 over 32 scores; no gap of 2 or more, so no third pass. Units are (video, setting) pairs; a run with no output (failed) scores instruction following 1 and is not scored on the other two. Scores are the mean of the two passes (1 to 5): F faithfulness, I instruction following, C coverage.

| Unit | Video         | Setting | gemma4:e4b F/I/C | qwen3.5:4b F/I/C  |
| ---- | ------------- | ------- | ---------------- | ----------------- |
| U01  | `bIvyxp-FAGU` | main    | 5.0/5.0/4.0      | 2.0/4.0/3.0       |
| U02  | `qovnfjVd8cs` | main    | 4.0/5.0/4.0      | no output (I = 1) |
| U03  | `M8_DnpYV2hY` | main    | 5.0/5.0/4.0      | 3.5/5.0/4.0       |
| U04  | `EfGtEDJyRvk` | main    | 4.0/3.0/4.0      | no output (I = 1) |
| U05  | `EfGtEDJyRvk` | ctx16k  | 4.5/3.5/4.0      | 2.0/2.0/3.5       |
| U06  | `eEE3S-MvzEs` | ctx16k  | 3.0/3.5/4.0      | 3.0/2.5/5.0       |

Means at 8192 (main): faithfulness gemma 4.5 over its four outputs against qwen 2.75 over its two (the only two qwen produced; on the same two videos, `bIvyxp-FAGU` and `M8_DnpYV2hY`, gemma scores 5.0 and qwen 2.75). Instruction following: gemma 4.5, qwen 2.75 counting failures as 1 (4.5 on the two it produced). Coverage: gemma 4.0, qwen 3.5. At 16384: faithfulness gemma 3.75 against qwen 2.5; instruction following 3.5 against 2.25; coverage 4.0 against 4.25. The sample is 6 units, two scorers; treat the differences as indicative.

Concrete errors the scorers found (derived text, no transcript quotes):

- qwen3.5:4b: on `bIvyxp-FAGU` it garbles the central claim (says most prostate cancers are "not diagnosed because they never die from it"), attributes PSA levels to vitamin D and insulin (which affect prostate size), and invents descriptions of the three PSA types; on `M8_DnpYV2hY` it garbles a "cyclical market" conclusion that applies to one company, distorts an 80%-of-revenue figure, and labels a quote with the wrong speaker; on `EfGtEDJyRvk` (16k) it invents a name for the Take-Two CEO, says the UnitedHealth chairman interviews the CEO, and attributes the host's points to the former UNH CEO; on `eEE3S-MvzEs` (16k) it misreads Tesla revenue as $224.9 billion (the transcript says 22.49 billion, a 12% decline), says Tesla's stock fell "rather than" for fundamental reasons although the host stresses fundamentals, and misstates a Google Cloud growth rate.
- gemma4:e4b: smaller errors: an advice sentence in the TLDR of `qovnfjVd8cs` that is not in the video and a loosely worded figure; a host opinion attributed to "some analysts" in `EfGtEDJyRvk`; in `eEE3S-MvzEs` (16k) it says retail investors "misjudge Google's stability" (the host says retail was right and analysts were wrong), reverses whose investment explains a list omission, and pads with a duplicate topic. Its instruction-following scores are lower on the longer videos (3.0 to 3.5); the scorers' notes cite duplicated topics, and every gemma TLDR had 5 or 6 sentences.
- Mechanical checks, from the driver (no LLM), for the outputs that were produced. No successful run needed a JSON repair, none had markdown or a preamble, and every TLDR was one paragraph:

| Output | TLDR sentences (rule 4 to 6) | Quotes verbatim in transcript | Speakers outside the roster (coerced) |
|---|---|---|---|
| gemma `bIvyxp-FAGU` | 6 | 60% | 0 |
| qwen `bIvyxp-FAGU` | 7 | 37.5% | 0 |
| gemma `qovnfjVd8cs` | 5 | 67% | 0 |
| gemma `M8_DnpYV2hY` | 6 | 80% | 0 |
| qwen `M8_DnpYV2hY` | 6 | 60% | 0 |
| gemma `EfGtEDJyRvk` (8192) | 5 | 90% | 2 |
| gemma `EfGtEDJyRvk` (16384) | 5 | 75% | 2 |
| qwen `EfGtEDJyRvk` (16384) | 3 | 73% | 7 |
| gemma `eEE3S-MvzEs` (16384) | 5 | 73% | 0 |
| qwen `eEE3S-MvzEs` (16384) | 5 | 54% | 3 |

Gemma quotes the transcript verbatim more often than qwen in every comparable pair.

### What was not done

- 5 of the 10 videos (YouTube 429; see above), so the video table is half the planned size.
- Repeats: one repeat only (gemma4:e4b on `qovnfjVd8cs`). The qwen repeats were skipped by the 3-failure rule and the rest were cancelled to make room for the `ctx16k` experiment. Each cell is a single run, so sampling noise is not measured beyond that one repeat (7% on wall time).
- The concurrent-memory step (a Whisper large-v3 container transcribing while a summary runs, with the 1 GB watchdog) was not done. The acceptance criteria asking for peak memory with a transcription running are therefore not met.
- The qwen3.5:4b re-run of `qovnfjVd8cs` was started at 08:03 and cancelled one minute later to run `ctx16k` on the long video with gemma instead; it has no result. The cause of qwen's first failure therefore stays undetermined.
- No cached-token field is reported by this Ollama version; the third scorer pass was not needed.
- The driver and acquisition scripts are not committed (scratch tooling kept outside the repository). `ytdigest eval report` covers the 8192 main runs only, because the `ctx16k` and repeat rows were removed from the throwaway database after each run (their outputs are in the run checkpoints, which are not committed).

### Recommendation (evidence-based advice pending the owner's decision)

This is advice from measured data, not a change: `OLLAMA_MODEL`, `.env.example`, `_docs/architecture.md` and the code are unchanged by this work.

The issue's proposed decision rule was: stay on `qwen3.5:4b` unless `gemma4:e4b` has at least 0.5 higher mean faithfulness or coverage, no more failed videos, and a concurrent-run minimum `MemAvailable` of at least 1 GB without the watchdog tripping.

- Faithfulness: met. Gemma scores 4.5 against 2.75 at 8192 and 3.75 against 2.5 at 16384 (coverage is level, 4.0 against 3.5 and 4.25). Qwen's errors are the kind a summarizer must not make: invented and swapped names and a wrong order of magnitude on a financial figure.
- Failures: met at the production setting. Gemma failed 1 of 5 videos at 8192 (a context overflow), qwen 3 of 5 (a timeout of undetermined cause, a context overflow and the 45 minute cap).
- Memory with a transcription running: not measured, so this condition cannot be confirmed. It is the main open item. Alone, gemma4:e4b used 12.3 to 13.1 GB of this 14 GB host (runner 7.6 to 8.2 GB), left 1.4 to 2.3 GB free and wrote up to 3.9 GB to swap in one run. The live transcriber (Whisper large-v3, limited to 6 GB) would run beside it. A trip of the 1 GB watchdog is likely, though this was not tested. Qwen3.5:4b left 4.0 to 5.8 GB free alone.

By the rule as written, the memory condition is unproven, so the rule does not yet license a move to gemma4:e4b. On quality alone the data favours gemma4:e4b clearly; on memory headroom it favours qwen3.5:4b clearly. The open question the owner has to settle, with one measurement, is whether gemma can run alongside Whisper on this host (or whether the analyzer and transcriber can be serialised so they never overlap, an option that was not tested).

Settings finding, independent of the model choice: with `OLLAMA_NUM_CTX` 8192 and `CHUNK_SEC` 900, 4 of 10 main runs failed or were cut, 2 of them by the context limit and both on a chunk prompt of 5.6k to 6.0k tokens; at `OLLAMA_NUM_CTX` 16384 all 4 runs succeeded. Raising `OLLAMA_NUM_CTX` to 16384, or shortening `CHUNK_SEC` so a chunk prompt stays well below the budget, is a candidate fix for both models. It costs memory (a larger KV cache, see the runner sizes above) and was tested on only two videos. Whichever way the owner goes, a follow-up measurement should include the concurrent Whisper step, more videos (the five that could not be fetched), repeats, and a re-run of the qwen timeout case.

## Status

Not decided. `OLLAMA_MODEL` stays `qwen3.5:4b`. The #382 results and an
evidence-based recommendation (pending the owner's decision) are in
"Real-transcript benchmark (issue #382)" above. Changing the default
needs the owner's approval and an update of `.env.example` and
`_docs/architecture.md`.
