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
| qwen3.5:4b on `qovnfjVd8cs` (see the round 2 note: a 900 s chunk-call timeout, as in round 2's rep 3) | failed after 1084 s                       | The second call (`analyze_chunk`) hit the 900 s call timeout (`TimeoutError`). No timing data exists for it. The host was under memory pressure at the time, but the sampler was not running yet, so the cause was undetermined in round 1 (superseded in round 2: the same failure recurs with ample memory, see "Round 2"): memory pressure and a runaway generation were both possible (a 4 tok/s generation runs past 900 s long before it reaches the context limit). |
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

Measured with the live transcriber stopped but the owner's desktop apps and the rest of the stack running. The concurrent step with Whisper was not done in round 1. Superseded in round 2: done, see "Round 2" below.

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
- Repeats: one repeat only (gemma4:e4b on `qovnfjVd8cs`). The qwen repeats were skipped by the 3-failure rule and the rest were cancelled to make room for the `ctx16k` experiment. Each cell is a single run, so sampling noise is not measured beyond that one repeat (7% on wall time). Superseded in round 2: three repeats per model were run, see below.
- The concurrent-memory step (a Whisper large-v3 container transcribing while a summary runs, with the 1 GB watchdog) was not done. The acceptance criteria asking for peak memory with a transcription running were therefore not met in round 1. Superseded in round 2: done, see below.
- The qwen3.5:4b re-run of `qovnfjVd8cs` was started at 08:03 and cancelled one minute later to run `ctx16k` on the long video with gemma instead; it has no result. The cause of qwen's first failure therefore stayed undetermined in round 1. Superseded in round 2: a qwen run on another video failed the same way (a 900 s chunk-call timeout) with ample memory and its normal chunk calls take 583 to 807 s, so this class of failure needs no memory explanation (see "Round 2" below).
- The cached-token value was not stored per call (see the cache check above); the third scorer pass was not needed.
- `ytdigest eval report` covers the 8192 main runs only, because the `ctx16k` and repeat rows were removed from the throwaway database after each run (their outputs are in the run checkpoints, which are not committed). The committed copy of the report has the per-chunk `Quotes` sections removed (verbatim transcript sentences); the full report exists only in the scratch directory of the run.
- The three repeats of video 1 per model (only one gemma repeat exists) and the concurrent-memory step were not done and are not faked: see "How to run the remaining steps" below. Superseded in round 2: done, see below.

### How to run the remaining steps

Superseded in round 2: the repeats and the concurrent-memory step are done (see "Round 2" below); only the five rate-limited videos remain. The text of this subsection is kept as the round 1 history, and the round 2 additions at its end correct it.

The scripts are in `_docs/ollama-benchmark-382/tools/` (stdlib plus the repo's own modules; excluded from ruff and mypy by `extend-exclude` and mypy `exclude` of `_docs`). They were written for a scratch directory `B` outside the repo: copy `env.sh.example` to `B/env.sh`, set the paths at the top of `run.sh`, `run_pairs.py` and the other scripts, start a throwaway Postgres on a private docker network without a volume, run `alembic upgrade head` against it, freeze transcripts with `acquire_all.sh <ids>` (one at a time, 30 s apart; YouTube may answer 429) and load them with `run.sh python load.py B <ids>`. A single run is `run.sh python run_one.py --model <m> --video <id> --label <label> --raw B/raw [--force] [--cap-sec S]`; `run_pairs.py` does the paired main protocol (shortest video first, order seeded 382); `mech.py`, `stats.py` and `make_packs.py` produce the checks, tables and blind packs; `fake_ollama.py` lets the whole chain be dry-run without a model.

- Superseded in round 2: done (video `bIvyxp-FAGU`, see below). Three repeats of video 1 per model (not done; one gemma repeat exists): with the live transcriber and analyzer stopped, for each model and `n` in 1 to 3 run `OLLAMA_HOST=172.17.0.1:11434 ollama stop <model>` and then `run.sh python run_one.py --model <model> --video qovnfjVd8cs --label rep<n> --force --raw B/raw`, alternating the model order. Run them directly: `run_pairs.py` skips a model that already has 3 failures (qwen has), and the repeats are reported separately, never averaged into the tables. About 15 minutes each, 90 minutes in all.
- Superseded in round 2: done, with a different video (`EfGtEDJyRvk`) and a working Whisper loop. Concurrent-memory step (not done): normalise the audio of `eEE3S-MvzEs` with the `YouTubeAudio` adapter in a throwaway `ytdigest-backend` container (read-only mount of the repo, scratch directory mounted for the output), then run the Whisper container `docker run --rm --name ytdigest-t382-whisper --cpus 3 --memory 6g -v ytdigest_whisper-models:/models:ro -e HF_HUB_OFFLINE=1 -e WHISPER_MODEL=large-v3 -e WHISPER_COMPUTE=int8 -e WHISPER_THREADS=3 <audio mount> --entrypoint python ytdigest-whisper:latest <loop>` where `<loop>` transcribes that file repeatedly with `FasterWhisperTranscriber` so Whisper stays busy for the whole summary run (that loop script is not provided and was not tested here). In parallel start `python3 tools/mem_watch.py --out B/logs/mem_concurrent.jsonl --whisper ytdigest-t382-whisper --pid <driver pid> --stop-file B/STOP` (it samples every 2 s and stops the container, the driver and the models if `MemAvailable` falls below 1 GB), then run the longest video once per model: `run.sh python run_one.py --model <model> --video eEE3S-MvzEs --label concurrent --force --cap-sec 4500 --raw B/raw` with `OLLAMA_NUM_CTX=16384` (8192 fails on this video). Compare the wall time with the clean `ctx16k` run above (2918 s qwen, 2932 s gemma). A watchdog trip is a result. Do this only with the live transcriber stopped, since it reproduces the transcriber's load.

Round 2 additions (2026-11-03 evening; details in "Round 2" below):

- The Whisper loop script was written for round 2 (`tools/round2/whisper_loop.py`, started by `tools/round2/phase2.py`). It must be given a `DATABASE_URL`: the faster-whisper child process calls `get_settings()`, which requires it, so without it every iteration fails at once and the container sits idle. A dummy value (`postgresql://none:none@127.0.0.1:1/none`) is enough because nothing connects. With it, `--network none`, a read-only `ytdigest_whisper-models` mount (`HF_HOME=/models`, `HF_HUB_OFFLINE=1`) and uid 10001 all work, and the container decodes at about 290% CPU.
- `tools/round2/phase2.py` is the resumable orchestrator for the three steps (concurrent memory, repeats, pairs), with the watchdog, the 23:30 hard stop and a `STOP` file; `acquire_patient.sh` is the slow acquirer with back-off. `run.sh` and the other scripts still carry the scratch paths of the machine they were written on.
- Do not use `pgrep -f tools/phase2.py` as an "already running" check from a shell that also runs a monitor with that string in its command line.
- Remaining after round 2: the five rate-limited videos (needs another night or a cookie file).

### Round 2 (issue #382, evening of 2026-11-03, label `r2-ctx16k`)

Run by the owner's decision of 2026-11-03 to schedule the steps round 1 did not run. Same host, same code (no change in `adapters/summarize`, `services/analyzer`, `services/eval` or `common/chunking` since round 1's commit `fa92b8c`; `round2/code_diff_since_round1.txt`), Ollama 0.40.13, the live analyzer's settings but with `OLLAMA_NUM_CTX` 16384 (the owner's chosen live value): reserve 2048, `CHUNK_SEC` 900, `OVERLAP_SEC` 60, prompt v1, `think` false, 900 s per call, 75 min per run. Every run was cold (models unloaded first). The live transcriber and analyzer were stopped, so the host was quiet (a browser and an IDE were still open: swap was already 3.5 to 4.4 GiB when the concurrent runs began). All memory figures in this section are GiB (kB divided by 1048576, as in the raw tables); the round 1 sections above use the unit of their own tables. Raw tables, per-run numbers and the host facts are in `_docs/ollama-benchmark-382/round2/`; the scripts are in `_docs/ollama-benchmark-382/tools/round2/`. Round 2 tables are never mixed with the round 1 tables above.

#### Headline: neither model can run next to Whisper large-v3 on this 14 GB host

The concurrent-memory step ran a summary of `EfGtEDJyRvk` (7.8k estimated tokens) while a throwaway Whisper container (`ytdigest-whisper:latest`, large-v3 int8, `--cpus 3 --memory 6g`, `WHISPER_THREADS=3`, model volume read-only, one real 1300 s speech file transcribed in a loop through the live `FasterWhisperTranscriber` child-process path) was decoding (in the qwen run at about 290% CPU and 3.2 to 4.1 GiB; in gemma's 46 s window the container's mean was about 170% CPU, minimum 15%, because it was still loading its model). The 1 GiB `MemAvailable` watchdog tripped for both models. Order was randomised (qwen first).

|                                          | qwen3.5:4b                                                                        | gemma4:e4b                                                      |
| ---------------------------------------- | --------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Outcome                                  | watchdog tripped at 1200 s (17:05:10, 0.88 GiB; mem_watch 0.74 GiB at 17:05:11)     | watchdog tripped at 46 s (17:06:47, 0.89 GiB), during model load |
| Swap used at start                       | 3.5 GiB                                                                            | 4.4 GiB                                                          |
| MemAvailable at start / minimum          | 6.0 GiB / 0.88 GiB                                                                  | 6.4 GiB / 0.89 GiB                                                |
| Peak swap                                | 7.6 GB                                                                            | 9.9 GiB (8.4 GiB within 21 s of the start)                        |
| Ollama runner RSS (peak)                 | 3.9 GiB                                                                            | 5.9 GiB (still growing when cut)                                 |
| Max PSI memory full (avg10)              | 4.2                                                                               | 40.5                                                            |
| Swap in / out during the run             | 2.1 GiB / 5.9 GiB                                                                   | 2.9 GiB / 7.9 GiB (in 46 s)                                       |
| Kernel `oom_kill` counter before / after | 1 / 1                                                                             | 1 / 1                                                           |
| Whisper container                        | stopped by the watchdog (exit 137, `OOMKilled` false)                             | removed after the trip, never restarted                         |
| Calls                                    | roster 307 s (alone: 166.0 s in the round 1 `ctx16k` run of the same video, 156.7 s in round 1 main), then the chunk call reached its 900 s timeout | none completed                                                  |

- qwen3.5:4b did not crash at once: with the runner at 3.7 to 3.9 GiB, `MemAvailable` sat at 1.5 to 2.1 GiB for 20 minutes, load average about 8, swap climbing from 3.5 to 7.1 GiB, and the first (roster) call took 307 s against 166.0 s alone for the same call of the same video (round 1 `ctx16k`; 156.7 s in round 1 main), about 1.85 times slower. It then fell under 1 GiB, so no summary could finish in the 900 s per call limit even if memory had held.
- gemma4:e4b was cut after 46 s during model load (the runner grew to 5.9 GiB, `MemAvailable` fell from 6.4 to 0.9 GiB, PSI full reached 40). Its numbers are a lower bound on what a full run would need; it was not given a second chance (the watchdog is the stop rule). The swap was already 4.4 GiB when it started, so the figures describe this host as it was used, not an empty machine.
- No kernel OOM kill was seen (`/proc/vmstat` `oom_kill`, since `dmesg` is not readable by this user) and the Whisper container was never `OOMKilled`. The watchdog stopped everything first, as designed.
- Whisper finished no full iteration in either window (the file is 1300 s of audio), so the figure that matters is that it stayed busy: about 290% CPU for the whole qwen run, and about 170% on average (minimum 15%) in gemma's 46 s window, which covered Whisper's own model load.

Consequence for the decision: the proposed rule's memory condition (concurrent minimum `MemAvailable` of at least 1 GiB without a watchdog trip) is not met by either model. Running the analyzer's Ollama job and the transcriber at the same time is not safe on this host, and gemma4:e4b is the worse of the two. A summary and a transcription must be serialised (or the host needs more memory), whichever model is chosen.

#### Repeats of `bIvyxp-FAGU` (11 min video, 3 repeats per model, `OLLAMA_NUM_CTX` 16384)

Order was randomised (seed `r2-rep`): qwen 3, gemma 3, gemma 2, gemma 1, qwen 1, qwen 2. Round 1's single runs (num_ctx 8192) are shown for comparison.

| run            | model      | status | wall s | roster / chunk / reduce call s | chunk out tokens | prefill / gen tok/s | min MemAvailable GiB | max PSI full10 | swap in / out MB |
| -------------- | ---------- | ------ | ------ | ------------------------------ | ---------------- | ------------------- | ------------------- | -------------- | ---------------- |
| round 1 (8192) | gemma4:e4b | ok     | 878    | 216 / 534 / 128                | 1872             | n/a                 | 1.6                 | n/a            | n/a              |
| round 1 (8192) | qwen3.5:4b | ok     | 904    | 194 / 583 / 127                | 1488             | n/a                 | 5.2                 | n/a            | n/a              |
| rep 1          | gemma4:e4b | ok     | 821    | 204 / 488 / 129                | 1826             | 21.5 / 6.9          | 2.1                 | 0.48           | 332 / 0          |
| rep 2          | gemma4:e4b | ok     | 912    | 206 / 558 / 148                | 2282             | 21.4 / 6.9          | 2.3                 | 7.28           | 254 / 691        |
| rep 3          | gemma4:e4b | ok     | 875    | 208 / 524 / 143                | 2044             | 21.3 / 6.8          | 1.6                 | 0.29           | 379 / 9          |
| rep 1          | qwen3.5:4b | ok     | 1151   | 180 / 807 / 163                | 2531             | 23.3 / 4.3          | 5.3                 | 2.02           | 178 / 183        |
| rep 2          | qwen3.5:4b | ok     | 1031   | 181 / 710 / 140                | 2143             | 23.2 / 4.3          | 5.1                 | 0.18           | 23 / 0           |
| rep 3          | qwen3.5:4b | failed | 1089   | 189 / 900 (timeout)            | none             | 22.4 / 5.1          | 5.3                 | 19.0           | 967 / 1          |

- Wall time: gemma4:e4b 821, 875, 912 s (mean 869 s, spread 91 s or about 10%); qwen3.5:4b 1151 and 1031 s on its two successes (mean 1091 s), 1.25 times gemma's. Round 1's single runs (878 and 904 s) sit inside or near the gemma range; qwen's 16k runs were slower than its 8k run, because its chunk output was longer (2143 and 2531 tokens against 1488).
- Failures: gemma 0 of 3, qwen 1 of 3 at 16384. Counting round 1's single 8192 runs too: gemma 4 of 4 and qwen 3 of 4 succeeded on this video.
- Corrected analysis of the qwen failure: rep 3 died on the chunk call's 900 s `TimeoutError` (`LLM_UNAVAILABLE`), with a minimum `MemAvailable` of 5.3 GiB, peak used 9.2 GiB and the runner at 4.3 GiB. It is not memory pressure. qwen3.5:4b generates at about 4.3 to 5.1 tokens/s and its chunk replies are long and uneven (1488, 2143 and 2531 tokens in the three successes, prefill about 4.8k tokens): the successful chunk calls took 583, 710 and 807 s, so the 900 s per-call limit is close to the model's normal range and a slightly longer reply times out. Round 1's qwen timeout on `qovnfjVd8cs` is the same kind of failure (a 900 s chunk call): a long reply near the limit is a sufficient cause, although that run also had memory pressure, so the round 1 run itself was not separated from it. gemma4:e4b's chunk calls took 488 to 558 s with 1826 to 2282 output tokens, well inside the limit.
- Mechanical output checks (the five 16k successes plus round 1; TLDR sentences use the corrected abbreviation-aware count, see the Correction above): TLDR sentences (rule 4 to 6) gemma 5, 6, 5 (round 1: 5), qwen 5, 5 (round 1: 6), so every TLDR is within the rule (the first draft of this section, with the old counter, wrongly said qwen broke it); quotes found verbatim in the transcript gemma 11 of 13, 13 of 16, 11 of 14 (round 1: 6 of 10), qwen 10 of 22, 7 of 13 (round 1: 3 of 8). No JSON repairs were needed in any run.
- Quality across the repeats (blind, two scorers, seven summaries of `bIvyxp-FAGU`: five round 2 successes plus the two round 1 runs; instruction following after the sentence-count re-score). Means of the two passes, F / I / C: gemma4:e4b (n = 4) 4.75 / 5.0 / 4.12; qwen3.5:4b (n = 3) 3.17 / 4.83 / 4.33. Scorer agreement on the pack: mean absolute difference 0.14, maximum 1, n = 21 scores. Run-to-run spread is about one point for both models: gemma's overall per-summary score (mean of the three criteria) runs from 4.0 to 5.0, qwen's from 3.3 to 4.5; faithfulness spans 4 to 5 for gemma and 2 to 4 for qwen. So gemma is ahead on faithfulness in every comparison, the two are close on instruction following, and coverage slightly favours qwen (4.33 against 4.12). Gemma's repeat wall times were 821, 875 and 912 s against qwen's 1031 and 1151 s plus one 900 s chunk-call timeout.

#### Not done in round 2

- The five videos YouTube rate-limited in round 1 (`PxP7IoagbtA`, `_Yt7NuD-nbk`, `_m1XcP1addc`, `hoYWNqh_u5I`, `HKskNSEb5_c`; reserves not tried): the subtitle request answered HTTP 429 on every attempt from 16:06 to the last attempt at 18:40 (the orchestrator stopped the acquirer at 18:46; about 2.5 hours, one video at a time, 45 s apart, back-off up to 30 min). Step 3 therefore did not run. It needs another night or a yt-dlp cookie file (issue #63). Superseded in rounds 3 and 4: four of the five were run after getting their transcripts from Whisper instead of captions, plus a substitute; `HKskNSEb5_c` was never covered (see "Rounds 3 and 4" below).
- The concurrent runs have no clean same-round baseline: the 7.8k-token video was run alone at 16384 only in round 1 (`ctx16k` rows above), so the slowdown of qwen's first call (307 s against 166.0 s for the same roster call alone, about 1.85 times) is compared across rounds.

### Rounds 3 and 4 (issue #382, 2026-11-04 and 2026-11-05, labels `r3-ctx16k` and `r4-ctx16k`)

Run to complete the owner's ten-video comparison: round 3 covered four videos with both models, round 4 six more. Same host, same code (the analyzer, adapter and chunking at `origin/master` 81c60f5), Ollama 0.40.13, the settings of round 2: `OLLAMA_NUM_CTX` 16384, reserve 2048, `CHUNK_SEC` 900, `OVERLAP_SEC` 60, prompt v1, `think` false, 900 s per call, 75 min per run, a cold start for every run (models unloaded, `/api/ps` empty), a sampler for PSI, swap, `MemAvailable` and the loaded model for the whole window, and the 1 GB `MemAvailable` watchdog (it never tripped: the runs were alone on the host). The live transcriber was stopped for both windows; the live analyzer only calls remote APIs. Model order per video was randomised from the seed `r3-ctx16k:<video>` (round 3) and `r4-ctx16k:<video>` (round 4); the stop rule was 3 failures per model. Raw tables, run records, scores with their private mappings, hashes and the scripts are in `_docs/ollama-benchmark-382/round3/`, `round4/` and `tools/round3/`, `tools/round4/`. Rounds 3 and 4 tables are never mixed with the earlier ones.

#### Transcripts

YouTube still answered HTTP 429 on the subtitle request throughout 2026-11-04 (and at 23:55, when round 4 started), but audio downloads worked. The transcripts were therefore made without the subtitle endpoint: the repo's `YouTubeAudio` adapter downloaded the audio and the repo's own `FasterWhisperTranscriber` ran Whisper large-v3 int8 in a throwaway container (`ytdigest-whisper:latest`, model volume read-only, `--network none`, 6 GB, a dummy `DATABASE_URL` for the child process, one video at a time and never beside an LLM run), so the segments have the shape of the live pipeline. Nine of the ten transcripts are Whisper: `PxP7IoagbtA`, `_Yt7NuD-nbk` and `_m1XcP1addc` (3 threads, round 3), `YjdfcHo_lE4` (exported read-only from the live API, where the live transcriber had produced it with Whisper) and `k1xJbNAGmFg`, `zVc-S0Od8_0`, `iM3YsKfRh6E`, `hoYWNqh_u5I`, `SMFV5A8aq0c` (6 CPUs and 6 threads, round 4; this differs from round 3 and, at these audio lengths, bought no clear speed-up). The tenth, `eeawNrRi-cc`, uses YouTube auto-captions: the subtitle endpoint answered again on 2026-11-05 at 07:06, when it was fetched. Whisper's wall time was 1.8 to 2.9 times the audio length (engine `rtf` 1.83, 2.34 and 2.60 with 3 threads; 1.81, 1.93, 2.37, 2.41 and 2.90 with 6 threads), much slower than the 1.14 of a 40 s trial, so on this host a transcript costs one to three times as long as a gemma summary of the same video (1971 to 4270 s of Whisper against 856 to 1696 s for one gemma run). Whisper transcripts here are lower case and unpunctuated, so the summaries of these videos are not strictly comparable with those of the caption-based videos of rounds 1 and 2.

#### The ten videos and the runs

The ten are: round 3 `PxP7IoagbtA`, `YjdfcHo_lE4`, `_Yt7NuD-nbk`, `_m1XcP1addc` (Sven Carlin, Sven Carlin, After Hours, BWB; 1046 to 1553 s), round 4 `k1xJbNAGmFg` (BWB, 1084 s), `zVc-S0Od8_0` (BWB, 1117 s), `iM3YsKfRh6E` (Dr. Berg, 1359 s), `hoYWNqh_u5I` (Sven Carlin, 1470 s), `SMFV5A8aq0c` (Joseph Carlson, 1550 s) and `eeawNrRi-cc` (After Hours, 1516 s, captions). `HKskNSEb5_c` (After Hours, 43 min), a planned video of the first rounds, was never covered: its transcript and runs did not fit the window, and `eeawNrRi-cc` replaced it so that the set reached ten.

| Video         | est tokens | gemma4:e4b | qwen3.5:4b          |
| ------------- | ---------- | ---------- | ------------------- |
| `PxP7IoagbtA` | 4639       | ok, 856 s  | ok, 1436 s          |
| `YjdfcHo_lE4` | 6743       | ok, 1263 s | ok, 1587 s          |
| `_Yt7NuD-nbk` | 7725       | ok, 1210 s | ok, 1722 s          |
| `_m1XcP1addc` | 8507       | ok, 1696 s | failed, 1091 s      |
| `k1xJbNAGmFg` | 6672       | ok, 1512 s | failed, 1060 s      |
| `zVc-S0Od8_0` | 6882       | ok, 1470 s | failed, 1075 s      |
| `iM3YsKfRh6E` | 7511       | ok, 1316 s | failed, 1075 s      |
| `hoYWNqh_u5I` | 6592       | ok, 1162 s | not run (stop rule) |
| `SMFV5A8aq0c` | 9565       | ok, 1204 s | not run (stop rule) |
| `eeawNrRi-cc` | 9120       | ok, 1417 s | not run (stop rule) |

That is 17 runs: gemma4:e4b completed 10 of 10; qwen3.5:4b completed 3 of 7 attempts and was then stopped by the 3-failure rule (round 4 held three qwen failures; the earlier one of round 3 is not counted towards that restart). Where both models ran on the same video, gemma was faster in the three completed pairs (856 against 1436 s, 1263 against 1587 s, 1210 against 1722 s): generation 6.3 to 7.7 tokens/s (all ten gemma runs) against 4.4 to 4.6 for qwen in the completed runs (about 5 in the failed ones), prefill about 22 against 24. Gemma used 11.9 to 12.1 GB at peak and left a minimum of 2.5 to 2.7 GB of `MemAvailable`, qwen 8.2 to 9.2 GB and 5.4 to 6.3 GB; swap reached 2.5 to 3.0 GB for both, with a maximum PSI full10 of 2.63 (gemma) and 1.59 (qwen). Per-call columns, parser counters and the sampler figures are in `round3/tables.md` and `round4/tables.md`.

#### Failures of qwen3.5:4b, with the cause

All four failures are the same event: the roster call succeeded (160 to 191 s, 18 to 21 output tokens), then the first chunk call hit the 900 s call timeout (`TimeoutError`, the adapter reports `LLM_UNAVAILABLE: Ollama ... did not answer`) and the run ended after 1060 to 1091 s with no timing data for the timed-out call. Memory was ample in every one (minimum `MemAvailable` 6.1 to 6.3 GB, PSI full10 at most 1.59, swap-in 1 to 13 MB), so it is not memory pressure. Why the call ran past 900 s was not observed. The circumstantial reading is that a chunk call needs about 6k tokens of prefill at about 24 tokens/s (around 250 s) and then qwen's usual 2000 to 3000 generated tokens at 4.4 to 5 tokens/s (400 to 650 s), so it sits close to the limit and a slightly longer generation crosses it; qwen's successful chunk calls in these rounds generated up to 3010 tokens. That is consistent with rounds 1 and 2 but unproven here. Whether a longer `OLLAMA_TIMEOUT_SEC` would let qwen finish was not tested.

#### Quality: scores

Scoring was blind as in the earlier rounds (rubric `RUBRIC.md`: faithfulness F, instruction following I, coverage C, integers 1 to 5; mechanical FACTS lines with the abbreviation-aware sentence counter; packs without model names; mappings kept apart and used only to un-blind). The scorers were LLM scorers of one model family, the same in both passes; no human scored. Raw scores and mappings: `round3/pass1.json`, `pass2.json`, `mapping_pass1.json`, `mapping_pass2.json` and `round4/abs1.json`, `abs2.json`, `mapping_abs1.json`, `mapping_abs2.json`.

Round 3, paired (X and Y shown together, two passes, mean of the two):

| Video           | gemma4:e4b F / I / C | qwen3.5:4b F / I / C    |
| --------------- | -------------------- | ----------------------- |
| `PxP7IoagbtA`   | 4.0 / 5.0 / 3.5      | 2.0 / 4.5 / 3.5         |
| `YjdfcHo_lE4`   | 4.0 / 4.5 / 3.5      | 2.0 / 4.5 / 4.0         |
| `_Yt7NuD-nbk`   | 4.0 / 5.0 / 4.0      | 3.5 / 3.0 / 4.0         |
| mean of these 3 | 4.0 / 4.8 / 3.7      | 2.5 / 4.0 / 3.8         |
| `_m1XcP1addc`   | 4.5 / 5.0 / 3.5      | no output (qwen failed) |

Over all four round-3 videos gemma scores 4.1 / 4.9 / 3.6 (8 scores per column). The faithfulness gap (gemma 4.0 against 2.5, 1.5 points) is of the size seen in rounds 1 and 2 (1.75 at 8192 and 1.25 at 16384 in the corrected scores); the scorers named the kind of error again for qwen: the wrong name for a company (EchoStar called "Ironwood"), a claim attributed to the wrong person, and a host's cautionary statement turned into its opposite ("liabilities are manageable", where the host calls $600 billion a risk). Coverage is level and instruction following favours gemma by 0.8 on these three videos; the one video where qwen came close on faithfulness is `_Yt7NuD-nbk` (3.5 against 4.0), and there it was also weaker on instruction following (3.0 against 5.0).

Round 4, absolute (one gemma summary per unit, no pair to compare with; both passes, mean of two):

| Video                    | F   | I   | C   |
| ------------------------ | --- | --- | --- |
| `iM3YsKfRh6E`            | 4.0 | 3.5 | 3.5 |
| `hoYWNqh_u5I`            | 4.0 | 4.0 | 4.0 |
| `SMFV5A8aq0c`            | 4.0 | 5.0 | 4.5 |
| `k1xJbNAGmFg`            | 4.0 | 4.0 | 4.0 |
| `zVc-S0Od8_0`            | 3.5 | 4.0 | 3.5 |
| `eeawNrRi-cc` (captions) | 4.0 | 4.5 | 4.5 |
| mean (12 scores)         | 3.9 | 4.2 | 4.0 |

#### Limitations

- Only three videos have a complete pair of summaries; for the other seven the comparison rests on qwen's failures (four of seven attempts), not on a quality contrast.
- One family of LLM scorers, two blind passes, no human; scores are integers on a three-criterion rubric and the two passes differ by one point in 16 of the 39 cells that both scored (never by more). The absolute scores of round 4 are softer than the paired ones, because a scorer without a second summary has nothing to be harsh against; they must not be set against the paired columns.
- Qwen's reliability is 3 completed runs of 7 attempted, and the cause of the slow first chunk call was not observed. A longer call timeout was not tried.
- One host, one Ollama version, one `OLLAMA_NUM_CTX` (16384). Videos are 17 to 26 minutes from five channels, mostly finance, English only.
- Nine transcripts are Whisper large-v3 int8 and one is auto-captions; Whisper ran with 3 threads in round 3 and 6 in round 4.
- `HKskNSEb5_c` was never covered; the ten were reached by substituting `eeawNrRi-cc`.
- The concurrent-memory finding (nothing runs beside Whisper) stays from round 2; rounds 3 and 4 ran with the jobs separated and did not re-test it.

### Recommendation (evidence-based advice pending the owner's decision)

This is advice from measured data, not a change: `OLLAMA_MODEL`, `.env.example`, `_docs/architecture.md` and the code are unchanged by this work.

The issue's proposed decision rule was: stay on `qwen3.5:4b` unless `gemma4:e4b` has at least 0.5 higher mean faithfulness or coverage, no more failed videos, and a concurrent-run minimum `MemAvailable` of at least 1 GB without the watchdog tripping.

- Faithfulness: met, and it is the stable difference after the sentence-count correction (instruction following is close). Gemma scores 4.5 against 2.75 at 8192 and 3.75 against 2.5 at 16384 (coverage is level, 4.0 against 3.5 and 4.25). Qwen's errors are the kind a summarizer must not make: invented and swapped names and a wrong order of magnitude on a financial figure.
- Failures: met at the production setting. Gemma failed 1 of 5 videos at 8192 (a context overflow), qwen 3 of 5 (a 900 s chunk-call timeout, a context overflow and the 45 minute cap). Qwen's chunk calls take 583 to 807 s when they succeed, so the 900 s limit sits close to its normal range; in round 2 one of three qwen repeats timed out with 5.3 GiB of `MemAvailable` to spare, and none of gemma's did.
- Memory with a transcription running: measured in round 2, and FAILED for both models. With a Whisper large-v3 container (3 CPUs, 6 GB) decoding beside a summary run of `EfGtEDJyRvk`, the 1 GB `MemAvailable` watchdog tripped for qwen3.5:4b after 1200 s (about 20 minutes of `MemAvailable` at 1.5 to 2.1 GiB with swap climbing from 3.5 to 7.6 GiB, then below 1 GiB) and for gemma4:e4b after 46 s, during model load (swap 4.4 to 9.9 GiB, PSI full10 40). No kernel OOM kill occurred and Whisper was never OOMKilled: the watchdog stopped everything first. Alone, gemma4:e4b used 12.3 to 13.1 GB of this 14 GB host (runner 7.6 to 8.2 GB, units of the round 1 memory table) and qwen3.5:4b 8.8 to 10.5 GB, so there is no headroom for a 3 to 4 GB transcriber beside either of them.

By the rule as written the memory condition is measured and failed for both models, so it does not separate them and does not license a move to gemma4:e4b by itself. What it does mean, whichever model is chosen: on this 14 GB host the analyzer's Ollama job and the transcriber must not run at the same time (serialise them, or add memory). On the quality data gemma4:e4b is clearly ahead on faithfulness (corrected scores; instruction following is close) and has fewer failures; on memory headroom alone qwen3.5:4b is better (4.0 to 5.8 GB free alone, against 1.4 to 2.3 GB). The choice between them is therefore a choice about quality against memory headroom with the two jobs serialised; the decision belongs to the owner.

Settings finding, independent of the model choice: with `OLLAMA_NUM_CTX` 8192 and `CHUNK_SEC` 900, 4 of 10 main runs failed or were cut, 2 of them by the context limit and both on a chunk prompt of 5.6k to 6.0k tokens; at `OLLAMA_NUM_CTX` 16384 all 4 runs succeeded. Raising `OLLAMA_NUM_CTX` to 16384, or shortening `CHUNK_SEC` so a chunk prompt stays well below the budget, is a candidate fix for both models. It costs memory (a larger KV cache, see the runner sizes above) and was tested on only two videos. Whichever way the owner goes, a follow-up measurement should include more videos (the five that could not be fetched) and, if the two jobs are ever to overlap, a host with more memory.

#### Update after rounds 3 and 4 (owner's decision, 2026-11-05)

The owner's decision, which this benchmark informs but does not make: use the free-tier remote summariser first, with a local Ollama model as the fallback, and keep the analyzer's Ollama jobs and the transcriber from ever running at the same time (#392, #393). The local fallback should be `gemma4:e4b` at `OLLAMA_NUM_CTX` 16384; `qwen3.5:4b` is not recommended.

- Why `gemma4:e4b`: it completed 10 of 10 videos at 16384 (qwen 3 of 7 attempts), was faster in all three completed pairs (856, 1263 and 1210 s against 1436, 1587 and 1722 s) and was more faithful in the only three complete blind pairs (4.0 against 2.5 on a 1 to 5 scale; coverage level, instruction following 4.8 against 4.0). Qwen's errors are invented or swapped names and reversed statements, and its first chunk call exceeded the 900 s limit in all four failures.
- What it costs: gemma4:e4b leaves a minimum of 2.5 to 2.7 GB of `MemAvailable` alone on this 14 GB host, so it cannot share the host with a transcription (round 2), which is why the serialisation is a requirement, not an option.
- What would change the answer: qwen with a longer call timeout is untested and might finish where it timed out (it is also slower and less faithful, so it would need to win on something else, for example memory headroom); a host with more memory would remove the serialisation; a larger or different model family, a human scorer or more paired videos would test the quality finding (three pairs and one scorer family are thin evidence). The choice of `OLLAMA_MODEL` stays with the owner.

## Status

The analyzer can now be run at `OLLAMA_NUM_CTX=16384` through `.env` (#389);
see "Labelled experiment" above.

Rounds 3 and 4 (ten videos in all) are in "Rounds 3 and 4" above; the
owner's decision they inform (free tier first, `gemma4:e4b` as the local
fallback at `OLLAMA_NUM_CTX` 16384, the analyzer's Ollama jobs and the
transcriber never overlapping, #392 and #393) is stated in "Update after
rounds 3 and 4". This document changes no default: `OLLAMA_MODEL` in the
code and `.env.example` stays `qwen3.5:4b` until the owner changes it. The #382 results and an
evidence-based recommendation (pending the owner's decision) are in
"Real-transcript benchmark (issue #382)" above. Changing the default
needs the owner's approval and an update of `.env.example` and
`_docs/architecture.md`.
