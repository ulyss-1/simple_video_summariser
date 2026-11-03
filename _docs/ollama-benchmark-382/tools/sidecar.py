"""Sidecar sampler: every 5 s append PSI, vmstat deltas, MemAvailable, SwapFree, loaded model to logs/sidecar.jsonl."""
import json
import time
import urllib.request

OUT = "/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/logs/sidecar.jsonl"


def psi():
    d = {}
    for line in open("/proc/pressure/memory"):
        kind, *kv = line.split()
        d[kind] = float(dict(x.split("=") for x in kv)["avg10"])
    return d


def vm():
    d = {}
    for line in open("/proc/vmstat"):
        k, v = line.split()
        if k in ("pswpin", "pswpout", "pgmajfault"):
            d[k] = int(v)
    return d


def mi():
    d = {}
    for line in open("/proc/meminfo"):
        k, _, v = line.partition(":")
        if k in ("MemAvailable", "SwapFree"):
            d[k] = int(v.split()[0])
    return d


prev = vm()
while True:
    time.sleep(5)
    cur = vm()
    try:
        ps = json.loads(urllib.request.urlopen("http://172.17.0.1:11434/api/ps", timeout=3).read())
        models = [m["name"] for m in ps.get("models", [])]
    except Exception:  # noqa: BLE001
        models = None
    p = psi()
    m = mi()
    rec = {"t": time.time(), "psi_some10": p.get("some"), "psi_full10": p.get("full"),
           "pswpin_pages": cur["pswpin"] - prev["pswpin"], "pswpout_pages": cur["pswpout"] - prev["pswpout"],
           "pgmajfault": cur["pgmajfault"] - prev["pgmajfault"], "avail_kb": m["MemAvailable"],
           "swapfree_kb": m["SwapFree"], "models": models}
    prev = cur
    with open(OUT, "a") as f:
        f.write(json.dumps(rec) + "\n")
