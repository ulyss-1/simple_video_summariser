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

## Status

Not decided. `OLLAMA_MODEL` stays `qwen3.5:4b`. Issue #382 runs both
candidates on 10 real transcripts, with peak-memory measurement while a
transcription runs, and ends with a recommendation. Changing the default
needs the owner's approval and an update of `.env.example` and
`_docs/architecture.md`.
