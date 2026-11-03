"""Step 1 timeline from raw/*conc*.result.json, logs/mem_concurrent_*.jsonl, logs/sidecar.jsonl, whisper logs -> out/conc.md"""
import json
from pathlib import Path

R = Path("/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round2")
side = [json.loads(x) for x in (R / "logs/sidecar.jsonl").read_text().splitlines() if x.strip()]
out = ["# Step 1: concurrent with Whisper large-v3 (EfGtEDJyRvk, num_ctx 16384)\n"]
for m in ("qwen3.5_4b", "gemma4_e4b"):
    r = json.loads((R / f"raw/r2-ctx16k-conc__EfGtEDJyRvk__{m}.result.json").read_text())
    note = json.loads((R / f"raw/r2-ctx16k-conc__EfGtEDJyRvk__{m}.conc_note.json").read_text())
    t0, t1 = r["started_epoch"], r["ended_epoch"]
    sw = [s for s in side if t0 <= s["t"] <= t1]
    mw = [json.loads(x) for x in (R / f"logs/mem_concurrent_{m}.jsonl").read_text().splitlines() if x.startswith("{")]
    trip = [x for x in (R / f"logs/mem_concurrent_{m}.jsonl").read_text().splitlines() if x.startswith("TRIPPED")]
    wl = [json.loads(x) for x in (R / f"logs/whisper_{m}.log").read_text().splitlines() if x.startswith("{")] if (R / f"logs/whisper_{m}.log").exists() else []
    swap_start = r["pre"]["meminfo_kb"]["SwapTotal"] - r["pre"]["meminfo_kb"]["SwapFree"]
    out.append(f"\n## {r['model']}\n")
    out.append(f"- status {r['status']}; run wall {r['wall_sec']:.0f} s; reason: {r.get('reason')}; mem_watch: {trip or 'no trip line'}")
    out.append(f"- calls {r['n_calls']}; settings {r['settings']}")
    out.append(f"- swap used at run start {swap_start/1048576:.1f} GB; MemAvailable at start {r['pre']['meminfo_kb']['MemAvailable']/1048576:.1f} GB; loadavg {r['pre']['loadavg']}")
    out.append(f"- driver minimum MemAvailable {r['mem']['min_avail_kb']/1048576:.2f} GB; peak used {r['mem']['peak_used_kb']/1048576:.1f} GB; peak swap {r['mem']['peak_swap_kb']/1048576:.1f} GB; peak Ollama runner RSS {r['mem']['peak_runner_kb']/1048576:.1f} GB")
    if sw:
        out.append(f"- sidecar: samples {len(sw)}; max PSI full10 {max(s['psi_full10'] for s in sw)}; max PSI some10 {max(s['psi_some10'] for s in sw)}; swap in {sum(s['pswpin_pages'] for s in sw)*4096/1e6:.0f} MB, out {sum(s['pswpout_pages'] for s in sw)*4096/1e6:.0f} MB; oom_kill total at end {sw[-1].get('oom_kill_total')}")
    out.append(f"- oom_kill counter before/after: {note['kernel_oom_kill_counter_before']} / {note['kernel_oom_kill_counter_after']}; whisper container exit state: {note['whisper_state_at_end'][:200]}")
    out.append(f"- whisper iterations completed: {len(wl)} ({[w.get('wall') for w in wl]}), errors {note['whisper_iteration_errors']}, restarts {note['whisper_events']}")
    out.append("\nTimeline (every ~60 s from the run's own samples: t s, MemAvailable GB, swap GB, runner RSS GB, load1):\n")
    sp = [json.loads(x) for x in (R / f"raw/r2-ctx16k-conc__EfGtEDJyRvk__{m}.samples.jsonl").read_text().splitlines() if x.strip()]
    step = max(1, len(sp) // 24)
    out.append("| t s | avail GB | swap GB | runner GB | load1 |\n|---|---|---|---|---|")
    for s in sp[::step] + sp[-1:]:
        out.append(f"| {s['t']} | {s['avail_kb']/1048576:.2f} | {s['swap_kb']/1048576:.1f} | {s['runner_rss_kb']/1048576:.1f} | {s['load1']} |")
(R / "out/conc.md").write_text("\n".join(out) + "\n")
print("\n".join(out))
