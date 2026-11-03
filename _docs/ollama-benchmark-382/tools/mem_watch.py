"""Memory watchdog for the concurrent step (and usable standalone).

mem_watch.py --out FILE [--whisper NAME] [--pid PID ...] [--stop-file F]
Every 1 s reads MemAvailable; below 1 GB it docker-stops the whisper container, SIGTERMs the pids,
unloads models via the ollama CLI, writes 'TRIPPED ...' to FILE and exits 3.
Every 2 s it appends a sample (MemAvailable, swap used, Ollama runner RSS, whisper `docker stats`).
Exits 0 when --stop-file appears.
"""
import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

LIMIT_KB = 1024 * 1024


def mem() -> dict[str, int]:
    d = {}
    for line in open("/proc/meminfo"):
        k, _, v = line.partition(":")
        d[k] = int(v.split()[0])
    return d


def runner_rss() -> int:
    ps = subprocess.run(["ps", "-eo", "rss=,args="], capture_output=True, text=True).stdout
    return max([int(line.split()[0]) for line in ps.splitlines() if "llama-server" in line or ("ollama" in line and "runner" in line)] or [0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--whisper")
    ap.add_argument("--pid", type=int, action="append", default=[])
    ap.add_argument("--stop-file")
    a = ap.parse_args()
    i = 0
    with open(a.out, "a") as f:
        while not (a.stop_file and Path(a.stop_file).exists()):
            m = mem()
            if m["MemAvailable"] < LIMIT_KB:
                f.write(f"TRIPPED {time.strftime('%F %T')} MemAvailable={m['MemAvailable']} kB\n")
                f.flush()
                if a.whisper:
                    subprocess.run(["docker", "stop", "-t", "5", a.whisper], capture_output=True)
                for pid in a.pid:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                ps = subprocess.run(["ollama", "ps"], capture_output=True, text=True).stdout.splitlines()[1:]
                for line in ps:
                    subprocess.run(["ollama", "stop", line.split()[0]], capture_output=True)
                return 3
            if i % 2 == 0:
                rec = {"t": time.strftime("%T"), "avail_kb": m["MemAvailable"],
                       "swap_kb": m["SwapTotal"] - m["SwapFree"], "runner_rss_kb": runner_rss()}
                if a.whisper:
                    st = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", a.whisper],
                                        capture_output=True, text=True).stdout.strip()
                    rec["whisper_stats"] = st
                f.write(json.dumps(rec) + "\n")
                f.flush()
            i += 1
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
