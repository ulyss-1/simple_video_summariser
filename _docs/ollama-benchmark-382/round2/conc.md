# Step 1: concurrent with Whisper large-v3 (EfGtEDJyRvk, num_ctx 16384)


## qwen3.5:4b

- status watchdog_tripped; run wall 1200 s; reason: MemAvailable 925648 kB at 17:05:10; mem_watch: ['TRIPPED 2026-11-03 17:05:11 MemAvailable=775452 kB']
- calls 1; settings {'OLLAMA_MODEL': 'qwen3.5:4b', 'OLLAMA_HOST': 'http://172.17.0.1:11434', 'OLLAMA_NUM_CTX': 16384, 'OLLAMA_OUTPUT_RESERVE_TOKENS': 2048, 'OLLAMA_TIMEOUT_SEC': 900, 'CHUNK_SEC': 900, 'OVERLAP_SEC': 60, 'PROMPT_VERSION': 'v1'}
- swap used at run start 3.5 GB; MemAvailable at start 6.0 GB; loadavg 1.94 1.97 2.80 6/1821 95730
- driver minimum MemAvailable 0.88 GB; peak used 13.4 GB; peak swap 7.6 GB; peak Ollama runner RSS 3.9 GB
- sidecar: samples 240; max PSI full10 4.17; max PSI some10 4.17; swap in 2098 MB, out 5865 MB; oom_kill total at end 1
- oom_kill counter before/after: 1 / 1; whisper container exit state: {"Status":"exited","Running":false,"Paused":false,"Restarting":false,"OOMKilled":false,"Dead":false,"Pid":0,"ExitCode":137,"Error":"","StartedAt":"2026-11-03T14:44:31.008365246Z","FinishedAt":"2026-11
- whisper iterations completed: 0 ([]), errors 0, restarts []

Timeline (every ~60 s from the run's own samples: t s, MemAvailable GB, swap GB, runner RSS GB, load1):

| t s | avail GB | swap GB | runner GB | load1 |
|---|---|---|---|---|
| 2.1 | 5.98 | 3.5 | 0.0 | 2.03 |
| 51.3 | 1.79 | 3.5 | 3.9 | 5.09 |
| 100.6 | 1.89 | 4.9 | 3.7 | 8.15 |
| 149.9 | 1.85 | 4.7 | 3.9 | 7.88 |
| 199.2 | 1.99 | 4.8 | 3.8 | 7.68 |
| 248.5 | 2.09 | 4.7 | 3.9 | 7.77 |
| 297.7 | 1.93 | 5.1 | 3.7 | 7.96 |
| 347.4 | 1.80 | 6.3 | 3.0 | 8.34 |
| 396.8 | 1.72 | 6.7 | 2.9 | 8.26 |
| 446.2 | 1.75 | 6.6 | 3.0 | 8.10 |
| 495.3 | 1.97 | 6.7 | 2.9 | 8.02 |
| 544.7 | 1.95 | 6.7 | 2.9 | 7.85 |
| 594.0 | 1.88 | 6.7 | 2.9 | 7.48 |
| 643.5 | 1.82 | 7.1 | 2.7 | 7.54 |
| 692.8 | 1.75 | 7.1 | 2.6 | 7.13 |
| 742.1 | 1.97 | 7.1 | 2.7 | 7.19 |
| 791.5 | 1.83 | 7.1 | 2.7 | 7.87 |
| 840.8 | 1.92 | 7.1 | 2.7 | 7.51 |
| 890.1 | 1.66 | 6.8 | 3.2 | 7.67 |
| 939.5 | 1.70 | 6.8 | 3.2 | 7.95 |
| 988.9 | 1.64 | 6.9 | 3.1 | 8.60 |
| 1038.3 | 1.90 | 6.9 | 3.1 | 7.91 |
| 1087.6 | 1.70 | 6.9 | 3.1 | 7.93 |
| 1137.0 | 1.75 | 6.9 | 3.1 | 7.94 |
| 1186.5 | 1.49 | 6.9 | 3.0 | 8.70 |
| 1200.9 | 1.15 | 7.6 | 2.9 | 8.58 |

## gemma4:e4b

- status watchdog_tripped; run wall 46 s; reason: MemAvailable 928192 kB at 17:06:47; mem_watch: no trip line
- calls 0; settings {'OLLAMA_MODEL': 'gemma4:e4b', 'OLLAMA_HOST': 'http://172.17.0.1:11434', 'OLLAMA_NUM_CTX': 16384, 'OLLAMA_OUTPUT_RESERVE_TOKENS': 2048, 'OLLAMA_TIMEOUT_SEC': 900, 'CHUNK_SEC': 900, 'OVERLAP_SEC': 60, 'PROMPT_VERSION': 'v1'}
- swap used at run start 4.4 GB; MemAvailable at start 6.4 GB; loadavg 6.37 7.54 6.54 5/1795 122177
- driver minimum MemAvailable 0.89 GB; peak used 13.3 GB; peak swap 9.9 GB; peak Ollama runner RSS 5.9 GB
- sidecar: samples 10; max PSI full10 40.47; max PSI some10 41.11; swap in 2916 MB, out 7885 MB; oom_kill total at end 1
- oom_kill counter before/after: 1 / 1; whisper container exit state: {"Status":"running","Running":true,"Paused":false,"Restarting":false,"OOMKilled":false,"Dead":false,"Pid":120864,"ExitCode":0,"Error":"","StartedAt":"2026-11-03T15:05:26.165802361Z","FinishedAt":"0001
- whisper iterations completed: 0 ([]), errors 0, restarts []

Timeline (every ~60 s from the run's own samples: t s, MemAvailable GB, swap GB, runner RSS GB, load1):

| t s | avail GB | swap GB | runner GB | load1 |
|---|---|---|---|---|
| 1.9 | 6.38 | 4.4 | 0.0 | 6.10 |
| 3.9 | 6.27 | 4.4 | 0.1 | 6.10 |
| 6.0 | 6.35 | 4.4 | 0.1 | 6.81 |
| 8.0 | 5.89 | 4.4 | 0.6 | 6.81 |
| 10.1 | 5.40 | 4.4 | 1.1 | 6.81 |
| 12.1 | 4.98 | 4.4 | 1.5 | 6.67 |
| 14.2 | 3.78 | 4.4 | 2.8 | 6.67 |
| 16.3 | 3.60 | 5.4 | 2.1 | 6.86 |
| 20.7 | 3.81 | 8.4 | 1.7 | 6.86 |
| 22.9 | 3.88 | 8.5 | 1.6 | 7.43 |
| 25.0 | 4.01 | 8.7 | 1.5 | 7.43 |
| 27.1 | 4.30 | 9.3 | 1.4 | 7.39 |
| 29.2 | 4.58 | 9.8 | 1.5 | 7.39 |
| 31.2 | 3.91 | 9.9 | 2.2 | 7.28 |
| 33.2 | 3.20 | 9.9 | 2.8 | 7.28 |
| 35.3 | 2.66 | 8.8 | 4.3 | 7.28 |
| 37.3 | 2.22 | 8.0 | 4.9 | 7.58 |
| 39.4 | 1.65 | 7.5 | 5.4 | 7.58 |
| 41.4 | 1.29 | 7.6 | 5.9 | 7.21 |
| 41.4 | 1.29 | 7.6 | 5.9 | 7.21 |
