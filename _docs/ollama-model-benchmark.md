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

Titles: `bIvyxp-FAGU` "Most Men Will Get Prostate Cancer As They Age (Here's What To Do)"; `qovnfjVd8cs` "Everything You Need to Know About Peptides"; `M8_DnpYV2hY` "The Last EASY Trade Before It Explodes"; `EfGtEDJyRvk` "United Health Group Stock Drops 50% and Moody's Lowers US Credit Rating"; `eEE3S-MvzEs` "Google Stock Will Keep Going Higher, Here's Why".

The model tokenizers count about the same as each other (3940 against 3862 prompt tokens for the roster call of `bIvyxp-FAGU`). Prompt tokens summed over all calls of a run (the model tokenizer's view; chunk calls plus the roster and reduce overhead; a failed run sums only the calls that completed):

| Video | Est. tokens | gemma4:e4b, 8192 | qwen3.5:4b, 8192 | gemma4:e4b, 16384 | qwen3.5:4b, 16384 |
|---|---|---|---|---|---|
| `bIvyxp-FAGU` | 3541 | 11017 | 10501 | - | - |
| `qovnfjVd8cs` | 3914 | 11526 (repeat 11305) | 3682 (failed) | - | - |
| `M8_DnpYV2hY` | 4258 | 10408 | 10144 | - | - |
| `EfGtEDJyRvk` | 7789 | 19582 | 9014 (failed) | 18253 | 17768 |
| `eEE3S-MvzEs` | 16186 | 16670 (failed) | 24273 (cap) | 34698 | 32374 |

The sums include about 3.4k to 3.9k tokens of roster call per run, so they are not a transcript size; the ratio of the repo estimator to the model count for the transcript alone is not separable from these records.

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

Cache check: the responses of Ollama 0.40.13 do list a `prompt_eval_cached_count` key (56 of the 57 recorded calls; the 57th is a call that timed out without a response). The driver stored the key names but not the value of that field, so "cached count = 0 for the first call" could not be verified per call from the records. What was verified instead: the first call of every run follows an unload with an empty `/api/ps`, and no call fell below the "suspected cache hit" threshold (prompt tokens below 50% of the estimated request tokens divided by 1.5), and the measured prefill rate of every first call (roster, 20 to 24 tok/s) matches the cold rate of the short-prompt measurement above, whereas a cached prefix would show an impossible rate. Treat the cold-prefill check as indirect.

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

### Host load before each run, and cleanup

`per_run_extra.md` lists, for every run, the load average read just before the run (it includes the previous run's tail, so it sits around 4.3 to 5.8 for most runs and 0.38 for the first), the highest CPU use among the other running containers at that moment (at most 5.0%: `db`, `api`, `planner`, `web`; the live transcriber and analyzer were stopped), prompt and output tokens summed, items dropped and `start_sec` clamped. The full `docker stats` snapshots are in each run's checkpoint (not committed).

Cleanup: at the end the throwaway Postgres container `ytdigest-t382-db` and network `ytdigest-t382-net` were removed (it had no volume); no `ytdigest-t382-*` object is left. The `ytdigest-*` image ids are the same before and after (`host_facts_before.txt`, `host_facts_after.txt`): `ytdigest-whisper:latest` b87bc3fb5329, `ytdigest-backend:latest` 6b10d5f28369, `ytdigest-web:latest` 08f098a80af9, `ytdigest-deps:latest` 81c55c4ea3b0. The volume `ytdigest_whisper-models` was never mounted, the live database `ytdigest` was never connected to, and `/api/ps` was empty at the end.

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
| U01  | `bIvyxp-FAGU` | main    | 5.0/5.0/4.0      | 2.0/5.0/3.0 (I was 4.0 before the re-score, see Correction) |
| U02  | `qovnfjVd8cs` | main    | 4.0/5.0/4.0      | no output (I = 1) |
| U03  | `M8_DnpYV2hY` | main    | 5.0/5.0/4.0      | 3.5/5.0/4.0       |
| U04  | `EfGtEDJyRvk` | main    | 4.0/3.0/4.0      | no output (I = 1) |
| U05  | `EfGtEDJyRvk` | ctx16k  | 4.5/3.5/4.0      | 2.0/2.0/3.5       |
| U06  | `eEE3S-MvzEs` | ctx16k  | 3.0/3.5/4.0      | 3.0/2.5/5.0       |

Means at 8192 (main), scored summaries only: faithfulness gemma 4.5 over its four outputs against qwen 2.75 over its two (the only two qwen produced; on the same two videos, `bIvyxp-FAGU` and `M8_DnpYV2hY`, gemma scores 5.0 and qwen 2.75). Instruction following (after the re-score of the Correction below): gemma 4.5, qwen 5.0 on its two outputs; counting the two qwen no-output runs that have a score row as 1, qwen 3.0 (mean over both passes and four units, n = 8). Coverage: gemma 4.0, qwen 3.5.

Over all five videos, applying the rubric's rule for failed runs (a job with no output scores instruction following 1; faithfulness and coverage are not scorable and are left out of those means), the 8192 instruction-following means include the two failed runs that had no score row, gemma on `eEE3S-MvzEs` and qwen on `eEE3S-MvzEs` (both 1):

| 8192, all 5 videos | gemma4:e4b | qwen3.5:4b |
|---|---|---|
| Instruction following, scored summaries only | 4.5 (4 outputs) | 5.0 (2 outputs) |
| Instruction following, no output = 1 | 3.8 (19 / 5) | 2.6 (13 / 5) |
| Faithfulness, scored summaries only | 4.5 (4 outputs) | 2.75 (2 outputs) |
| Coverage, scored summaries only | 4.0 (4 outputs) | 3.5 (2 outputs) |
| Videos with no summary | 1 of 5 | 3 of 5 |

On the corrected data the two models are close on instruction following when both produce a summary; the stable difference is faithfulness (and failures), not format. The scored-only means compare different sets of videos (gemma 4, qwen 2), which is why the failure counts are shown beside them. At 16384: faithfulness gemma 3.75 against qwen 2.5; instruction following 3.5 against 2.25; coverage 4.0 against 4.25. The sample is 6 units, two scorers; treat the differences as indicative.

Concrete errors the scorers found (derived text, no transcript quotes):

- qwen3.5:4b: on `bIvyxp-FAGU` it garbles the central claim (says most prostate cancers are "not diagnosed because they never die from it"), attributes PSA levels to vitamin D and insulin (which affect prostate size), and invents descriptions of the three PSA types; on `M8_DnpYV2hY` it garbles a "cyclical market" conclusion that applies to one company, distorts an 80%-of-revenue figure, and labels a quote with the wrong speaker; on `EfGtEDJyRvk` (16k) it invents a name for the Take-Two CEO, says the UnitedHealth chairman interviews the CEO, and attributes the host's points to the former UNH CEO; on `eEE3S-MvzEs` (16k) it misreads Tesla revenue as $224.9 billion (the transcript says 22.49 billion, a 12% decline), says Tesla's stock fell "rather than" for fundamental reasons although the host stresses fundamentals, and misstates a Google Cloud growth rate.
- gemma4:e4b: smaller errors: an advice sentence in the TLDR of `qovnfjVd8cs` that is not in the video and a loosely worded figure; a host opinion attributed to "some analysts" in `EfGtEDJyRvk`; in `eEE3S-MvzEs` (16k) it says retail investors "misjudge Google's stability" (the host says retail was right and analysts were wrong), reverses whose investment explains a list omission, and pads with a duplicate topic. Its instruction-following scores are lower on the longer videos (3.0 to 3.5); the scorers' notes cite duplicated topics, and every gemma TLDR had 5 or 6 sentences.
- Mechanical checks, from the driver (no LLM), for the outputs that were produced. `start_sec` values clamped into the chunk span: 0 in most runs (gemma 1 on `EfGtEDJyRvk` at 8192 and 1 at 16384, 3 on `eEE3S-MvzEs` at 16384; qwen 2 on `EfGtEDJyRvk` and 5 on `eEE3S-MvzEs` at 16384, 2 in the failed-then-capped 8192 run of `eEE3S-MvzEs`); items dropped by the parser: 0 in every run (per run in `per_run_extra.md`). No successful run needed a JSON repair, none had markdown or a preamble, and every TLDR was one paragraph:

| Output | TLDR sentences (rule 4 to 6) | Quotes verbatim in transcript | Speakers outside the roster (coerced) |
|---|---|---|---|
| gemma `bIvyxp-FAGU` | 5 (first count 6, see Correction) | 60% | 0 |
| qwen `bIvyxp-FAGU` | 6 (first count 7, see Correction) | 37.5% | 0 |
| gemma `qovnfjVd8cs` | 5 | 67% | 0 |
| gemma `M8_DnpYV2hY` | 6 | 80% | 0 |
| qwen `M8_DnpYV2hY` | 6 | 60% | 0 |
| gemma `EfGtEDJyRvk` (8192) | 5 | 90% | 2 |
| gemma `EfGtEDJyRvk` (16384) | 5 | 75% | 2 |
| qwen `EfGtEDJyRvk` (16384) | 3 | 73% | 7 |
| gemma `eEE3S-MvzEs` (16384) | 5 | 73% | 0 |
| qwen `eEE3S-MvzEs` (16384) | 5 | 54% | 3 |

Gemma quotes the transcript verbatim more often than qwen in every comparable pair.

#### Correction (added in round 2): the TLDR sentence counter treated "Dr." as a sentence end

What was wrong: the mechanical counter (`tools/mech.py`) split a TLDR at every full stop followed by a capital, so "Dr. Eric Berg" counted as a sentence end. Every summary of `bIvyxp-FAGU` (a Dr. Eric Berg video) was over-counted by one or more. The round 1 table above said 6 (gemma) and 7 (qwen); the abbreviation-aware recount (`tools/sentences.py`, with a self-check on the "Dr. Eric Berg" sentence) gives 5 and 6. All other round 1 outputs are unchanged (recount for every run: `round2/sentence_recount.json`). The scorers saw the inflated number in the `FACTS (mechanical)` line of the pack.

What it changes: qwen's `bIvyxp-FAGU` TLDR was within the 4 to 6 rule, not "7 sentences, one minor deviation", and the same holds for the round 2 repeats (their first-draft count of 7 to 8 is withdrawn). The qwen summaries whose score could have been set by the wrong count (round 1 `bIvyxp-FAGU` in both passes, and repeat-pack S4, S5 and S7) got a measured re-score of instruction following only: two fresh blind scorers, working from corrected packs (same X/Y and S labels, only the sentence count in the FACTS line changed). Result: every re-scored summary got instruction following 5 from both scorers, except repeat summary S5 (qwen rep 1), which got 4 and 5 (scorer A: 22 quotes of which only 10 are verbatim, list padding). Faithfulness and coverage do not use the sentence count and were not re-scored. Gemma's scores are unchanged (its counts stayed inside the rule).

Corrected figures, means over both passes (no output counts 1 on instruction following only):

| Setting | gemma4:e4b F / I / C | qwen3.5:4b F / I / C |
|---|---|---|
| 8192, main (5 videos; qwen I counts its two no-output runs, n = 8) | 4.5 / 4.5 / 4.0 | 2.75 / 3.0 / 3.5 |
| 16384, ctx16k (U05, U06) | 3.75 / 3.5 / 4.0 | 2.5 / 2.25 / 4.25 |

Before the correction the 8192 qwen instruction-following mean was 2.75 and the all-five mean 2.4 (now 3.0 and, in the table above, 2.6). On the corrected data, **the two models are close on instruction following; the stable difference is faithfulness, where gemma is ahead in every comparison; coverage slightly favours qwen where both ran at 16384** (4.25 against 4.0 here, 4.33 against 4.12 in the repeats; at 8192 it is 3.5 against 4.0 in gemma's favour, on only two qwen outputs). Failures remain a second difference (qwen 3 of 5 videos at 8192 against gemma 1 of 5).

### What was not done

- 5 of the 10 videos (YouTube 429; see above), so the video table is half the planned size.
- Repeats: one repeat only (gemma4:e4b on `qovnfjVd8cs`). The qwen repeats were skipped by the 3-failure rule and the rest were cancelled to make room for the `ctx16k` experiment. Each cell is a single run, so sampling noise is not measured beyond that one repeat (7% on wall time).
- The concurrent-memory step (a Whisper large-v3 container transcribing while a summary runs, with the 1 GB watchdog) was not done. The acceptance criteria asking for peak memory with a transcription running are therefore not met.
- The qwen3.5:4b re-run of `qovnfjVd8cs` was started at 08:03 and cancelled one minute later to run `ctx16k` on the long video with gemma instead; it has no result. The cause of qwen's first failure therefore stays undetermined.
- The cached-token value was not stored per call (see the cache check above); the third scorer pass was not needed.
- `ytdigest eval report` covers the 8192 main runs only, because the `ctx16k` and repeat rows were removed from the throwaway database after each run (their outputs are in the run checkpoints, which are not committed). The committed copy of the report has the per-chunk `Quotes` sections removed (verbatim transcript sentences); the full report exists only in the scratch directory of the run.
- The three repeats of video 1 per model (only one gemma repeat exists) and the concurrent-memory step were not done and are not faked: see "How to run the remaining steps" below.

### How to run the remaining steps

The scripts are in `_docs/ollama-benchmark-382/tools/` (stdlib plus the repo's own modules; excluded from ruff and mypy by `extend-exclude` and mypy `exclude` of `_docs`). They were written for a scratch directory `B` outside the repo: copy `env.sh.example` to `B/env.sh`, set the paths at the top of `run.sh`, `run_pairs.py` and the other scripts, start a throwaway Postgres on a private docker network without a volume, run `alembic upgrade head` against it, freeze transcripts with `acquire_all.sh <ids>` (one at a time, 30 s apart; YouTube may answer 429) and load them with `run.sh python load.py B <ids>`. A single run is `run.sh python run_one.py --model <m> --video <id> --label <label> --raw B/raw [--force] [--cap-sec S]`; `run_pairs.py` does the paired main protocol (shortest video first, order seeded 382); `mech.py`, `stats.py` and `make_packs.py` produce the checks, tables and blind packs; `fake_ollama.py` lets the whole chain be dry-run without a model.

- Three repeats of video 1 per model (not done; one gemma repeat exists): with the live transcriber and analyzer stopped, for each model and `n` in 1 to 3 run `OLLAMA_HOST=172.17.0.1:11434 ollama stop <model>` and then `run.sh python run_one.py --model <model> --video qovnfjVd8cs --label rep<n> --force --raw B/raw`, alternating the model order. Run them directly: `run_pairs.py` skips a model that already has 3 failures (qwen has), and the repeats are reported separately, never averaged into the tables. About 15 minutes each, 90 minutes in all.
- Concurrent-memory step (not done): normalise the audio of `eEE3S-MvzEs` with the `YouTubeAudio` adapter in a throwaway `ytdigest-backend` container (read-only mount of the repo, scratch directory mounted for the output), then run the Whisper container `docker run --rm --name ytdigest-t382-whisper --cpus 3 --memory 6g -v ytdigest_whisper-models:/models:ro -e HF_HUB_OFFLINE=1 -e WHISPER_MODEL=large-v3 -e WHISPER_COMPUTE=int8 -e WHISPER_THREADS=3 <audio mount> --entrypoint python ytdigest-whisper:latest <loop>` where `<loop>` transcribes that file repeatedly with `FasterWhisperTranscriber` so Whisper stays busy for the whole summary run (that loop script is not provided and was not tested here). In parallel start `python3 tools/mem_watch.py --out B/logs/mem_concurrent.jsonl --whisper ytdigest-t382-whisper --pid <driver pid> --stop-file B/STOP` (it samples every 2 s and stops the container, the driver and the models if `MemAvailable` falls below 1 GB), then run the longest video once per model: `run.sh python run_one.py --model <model> --video eEE3S-MvzEs --label concurrent --force --cap-sec 4500 --raw B/raw` with `OLLAMA_NUM_CTX=16384` (8192 fails on this video). Compare the wall time with the clean `ctx16k` run above (2918 s qwen, 2932 s gemma). A watchdog trip is a result. Do this only with the live transcriber stopped, since it reproduces the transcriber's load.

Round 2 additions (2026-11-03 evening; details in "Round 2" below):

- The Whisper loop script was written for round 2 (`tools/round2/whisper_loop.py`, started by `tools/round2/phase2.py`). It must be given a `DATABASE_URL`: the faster-whisper child process calls `get_settings()`, which requires it, so without it every iteration fails at once and the container sits idle. A dummy value (`postgresql://none:none@127.0.0.1:1/none`) is enough because nothing connects. With it, `--network none`, a read-only `ytdigest_whisper-models` mount (`HF_HOME=/models`, `HF_HUB_OFFLINE=1`) and uid 10001 all work, and the container decodes at about 290% CPU.
- `tools/round2/phase2.py` is the resumable orchestrator for the three steps (concurrent memory, repeats, pairs), with the watchdog, the 23:30 hard stop and a `STOP` file; `acquire_patient.sh` is the slow acquirer with back-off. `run.sh` and the other scripts still carry the scratch paths of the machine they were written on.
- Do not use `pgrep -f tools/phase2.py` as an "already running" check from a shell that also runs a monitor with that string in its command line.
- Remaining after round 2: the five rate-limited videos (needs another night or a cookie file) and the blind scores of the repeats.

### Round 2 (issue #382, evening of 2026-11-03, label `r2-ctx16k`)

Run by the owner's decision of 2026-11-03 to schedule the steps round 1 did not run. Same host, same code (no change in `adapters/summarize`, `services/analyzer`, `services/eval` or `common/chunking` since round 1's commit `fa92b8c`; `round2/code_diff_since_round1.txt`), Ollama 0.40.13, the live analyzer's settings but with `OLLAMA_NUM_CTX` 16384 (the owner's chosen live value): reserve 2048, `CHUNK_SEC` 900, `OVERLAP_SEC` 60, prompt v1, `think` false, 900 s per call, 75 min per run. Every run was cold (models unloaded first). The live transcriber and analyzer were stopped, so the host was quiet (a browser and an IDE were still open: swap was already 3.5 to 4.4 GB when the concurrent runs began). Raw tables, per-run numbers and the host facts are in `_docs/ollama-benchmark-382/round2/`; the scripts are in `_docs/ollama-benchmark-382/tools/round2/`. Round 2 tables are never mixed with the round 1 tables above.

#### Headline: neither model can run next to Whisper large-v3 on this 14 GB host

The concurrent-memory step ran a summary of `EfGtEDJyRvk` (7.8k estimated tokens) while a throwaway Whisper container (`ytdigest-whisper:latest`, large-v3 int8, `--cpus 3 --memory 6g`, `WHISPER_THREADS=3`, model volume read-only, one real 1300 s speech file transcribed in a loop through the live `FasterWhisperTranscriber` child-process path) was decoding at about 290% CPU and 3.2 to 4.1 GiB. The 1 GB `MemAvailable` watchdog tripped for both models. Order was randomised (qwen first).

|                                          | qwen3.5:4b                                                                        | gemma4:e4b                                                      |
| ---------------------------------------- | --------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Outcome                                  | watchdog tripped at 1200 s (17:05:10, 0.93 GB; mem_watch 0.78 GB at 17:05:11)     | watchdog tripped at 46 s (17:06:47, 0.93 GB), during model load |
| Swap used at start                       | 3.5 GB                                                                            | 4.4 GB                                                          |
| MemAvailable at start / minimum          | 6.0 GB / 0.88 GB                                                                  | 6.4 GB / 0.89 GB                                                |
| Peak swap                                | 7.6 GB                                                                            | 9.9 GB (8.4 GB within 21 s of the start)                        |
| Ollama runner RSS (peak)                 | 3.9 GB                                                                            | 5.9 GB (still growing when cut)                                 |
| Max PSI memory full (avg10)              | 4.2                                                                               | 40.5                                                            |
| Swap in / out during the run             | 2.1 GB / 5.9 GB                                                                   | 2.9 GB / 7.9 GB (in 46 s)                                       |
| Kernel `oom_kill` counter before / after | 1 / 1                                                                             | 1 / 1                                                           |
| Whisper container                        | stopped by the watchdog (exit 137, `OOMKilled` false)                             | removed after the trip, never restarted                         |
| Calls                                    | roster 307 s (alone: 180 to 190 s), then the chunk call reached its 900 s timeout | none completed                                                  |

- qwen3.5:4b did not crash at once: with the runner at 3.7 to 3.9 GB, `MemAvailable` sat at 1.5 to 2.1 GB for 20 minutes, load average about 8, swap climbing from 3.5 to 7.1 GB, and the first call ran 1.6 times slower than alone. It then fell under 1 GB, so no summary could finish in the 900 s per call limit even if memory had held.
- gemma4:e4b was cut after 46 s during model load (the runner grew to 5.9 GB, `MemAvailable` fell from 6.4 to 0.9 GB, PSI full reached 40). Its numbers are a lower bound on what a full run would need; it was not given a second chance (the watchdog is the stop rule). The swap was already 4.4 GB when it started, so the figures describe this host as it was used, not an empty machine.
- No kernel OOM kill was seen (`/proc/vmstat` `oom_kill`, since `dmesg` is not readable by this user) and the Whisper container was never `OOMKilled`. The watchdog stopped everything first, as designed.
- Whisper finished no full iteration in either window (the file is 1300 s of audio), so the figure that matters here is that it stayed busy at about 290% CPU for the whole summary run.

Consequence for the decision: the proposed rule's memory condition (concurrent minimum `MemAvailable` of at least 1 GB without a watchdog trip) is not met by either model. Running the analyzer's Ollama job and the transcriber at the same time is not safe on this host, and gemma4:e4b is the worse of the two. A summary and a transcription must be serialised (or the host needs more memory), whichever model is chosen.

#### Repeats of `bIvyxp-FAGU` (11 min video, 3 repeats per model, `OLLAMA_NUM_CTX` 16384)

Order was randomised (seed `r2-rep`): qwen 3, gemma 3, gemma 2, gemma 1, qwen 1, qwen 2. Round 1's single runs (num_ctx 8192) are shown for comparison.

| run            | model      | status | wall s | roster / chunk / reduce call s | chunk out tokens | prefill / gen tok/s | min MemAvailable GB | max PSI full10 | swap in / out MB |
| -------------- | ---------- | ------ | ------ | ------------------------------ | ---------------- | ------------------- | ------------------- | -------------- | ---------------- |
| round 1 (8192) | gemma4:e4b | ok     | 878    | 216 / 534 / 128                | 1872             | n/a                 | 1.6                 | n/a            | n/a              |
| round 1 (8192) | qwen3.5:4b | ok     | 904    | 194 / 583 / 127                | 1488             | n/a                 | 5.2                 | n/a            | n/a              |
| rep 1          | gemma4:e4b | ok     | 821    | 204 / 488 / 129                | 1826             | 21.5 / 6.9          | 2.1                 | 0.48           | 332 / 0          |
| rep 2          | gemma4:e4b | ok     | 912    | 206 / 558 / 148                | 2282             | 21.4 / 6.9          | 2.3                 | 7.28           | 254 / 691        |
| rep 3          | gemma4:e4b | ok     | 875    | 208 / 524 / 143                | 2044             | 21.3 / 6.8          | 1.6                 | 0.29           | 379 / 9          |
| rep 1          | qwen3.5:4b | ok     | 1151   | 180 / 807 / 163                | 2531             | 23.3 / 4.3          | 5.3                 | 2.02           | 178 / 183        |
| rep 2          | qwen3.5:4b | ok     | 1031   | 181 / 710 / 140                | 2143             | 23.2 / 4.3          | 5.1                 | 0.18           | 23 / 0           |
| rep 3          | qwen3.5:4b | failed | 1089   | 189 / 900 (timeout)            | none             | 22.4 / 5.1          | 5.5                 | 19.0           | 967 / 1          |

- Wall time: gemma4:e4b 821, 875, 912 s (mean 869 s, spread 91 s or about 10%); qwen3.5:4b 1151 and 1031 s on its two successes (mean 1091 s), 1.25 times gemma's. Round 1's single runs (878 and 904 s) sit inside or near the gemma range; qwen's 16k runs were slower than its 8k run, because its chunk output was longer (2143 and 2531 tokens against 1488).
- Failures: gemma 0 of 3, qwen 1 of 3 at 16384. Counting round 1's single 8192 runs too: gemma 4 of 4 and qwen 3 of 4 succeeded on this video.
- Corrected analysis of the qwen failure: rep 3 died on the chunk call's 900 s `TimeoutError` (`LLM_UNAVAILABLE`), with a minimum `MemAvailable` of 5.5 GB, peak used 9.7 GB and the runner at 4.5 GB. It is not memory pressure. qwen3.5:4b generates at about 4.3 to 5.1 tokens/s and its chunk replies are long and uneven (1488, 2143 and 2531 tokens in the three successes, prefill about 4.8k tokens): the successful chunk calls took 583, 710 and 807 s, so the 900 s per-call limit is close to the model's normal range and a slightly longer reply times out. The same reading applies to round 1's qwen timeout on `qovnfjVd8cs` that round 1 left "undetermined": a long reply near the limit is a sufficient cause, so a memory attribution there was never needed. gemma4:e4b's chunk calls took 488 to 558 s with 1826 to 2282 output tokens, well inside the limit.
- Mechanical output checks (the five 16k successes plus round 1; TLDR sentences use the corrected abbreviation-aware count, see the Correction above): TLDR sentences (rule 4 to 6) gemma 5, 6, 5 (round 1: 5), qwen 5, 5 (round 1: 6), so every TLDR is within the rule (the first draft of this section, with the old counter, wrongly said qwen broke it); quotes found verbatim in the transcript gemma 11 of 13, 13 of 16, 11 of 14 (round 1: 6 of 10), qwen 10 of 22, 7 of 13 (round 1: 3 of 8). No JSON repairs were needed in any run.
- Quality across the repeats (blind, two scorers, seven summaries of `bIvyxp-FAGU`: five round 2 successes plus the two round 1 runs; instruction following after the sentence-count re-score). Means of the two passes, F / I / C: gemma4:e4b (n = 4) 4.75 / 5.0 / 4.12; qwen3.5:4b (n = 3) 3.17 / 4.83 / 4.33. Scorer agreement on the pack: mean absolute difference 0.14, maximum 1, n = 21 scores. Run-to-run spread is about one point for both models: gemma's overall per-summary score (mean of the three criteria) runs from 4.0 to 5.0, qwen's from 3.3 to 4.5; faithfulness spans 4 to 5 for gemma and 2 to 4 for qwen. So gemma is ahead on faithfulness in every comparison, the two are close on instruction following, and coverage slightly favours qwen (4.33 against 4.12). Gemma's repeat wall times were 821, 875 and 912 s against qwen's 1031 and 1151 s plus one 900 s chunk-call timeout.

#### Not done in round 2

- The five videos YouTube rate-limited in round 1 (`PxP7IoagbtA`, `_Yt7NuD-nbk`, `_m1XcP1addc`, `hoYWNqh_u5I`, `HKskNSEb5_c`; reserves not tried): the subtitle request answered HTTP 429 on every attempt from 16:06 to 18:46 (about 2.5 hours, one video at a time, 45 s apart, back-off up to 30 min). Step 3 therefore did not run. It needs another night or a yt-dlp cookie file (issue #63).
- The concurrent runs have no clean same-round baseline: the 7.8k-token video was run alone at 16384 only in round 1 (`ctx16k` rows above), so the slowdown of qwen's first call (307 s against 180 to 190 s for the same roster call alone) is compared across rounds.

### Recommendation (evidence-based advice pending the owner's decision)

This is advice from measured data, not a change: `OLLAMA_MODEL`, `.env.example`, `_docs/architecture.md` and the code are unchanged by this work.

The issue's proposed decision rule was: stay on `qwen3.5:4b` unless `gemma4:e4b` has at least 0.5 higher mean faithfulness or coverage, no more failed videos, and a concurrent-run minimum `MemAvailable` of at least 1 GB without the watchdog tripping.

- Faithfulness: met, and it is the stable difference after the sentence-count correction (instruction following is close). Gemma scores 4.5 against 2.75 at 8192 and 3.75 against 2.5 at 16384 (coverage is level, 4.0 against 3.5 and 4.25). Qwen's errors are the kind a summarizer must not make: invented and swapped names and a wrong order of magnitude on a financial figure.
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
