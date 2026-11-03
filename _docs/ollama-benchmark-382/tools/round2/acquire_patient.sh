#!/bin/bash
# Patient acquisition: one video at a time, >=45 s between attempts; after a round with no success,
# back off 2,4,8,16,30,30... min (429). Stops when all ids are frozen, or when the file $R/ACQ_STOP exists.
R=/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round2
W=/home/hamlet/proj/simple_video_summariser/.claude/worktrees/agent-aaf2b01791212b49d
backoff=120
while :; do
  missing=0; got=0
  for v in "$@"; do
    [ -f "$R/ACQ_STOP" ] && { echo "ACQ_STOP $(date +%T)" >> "$R/logs/acquire.log"; exit 0; }
    [ -f "$R/frozen/$v.segments.json" ] && continue
    missing=1
    if docker run --rm --name "ytdigest-t382b-acq-${v//[^A-Za-z0-9]/}" --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$W":/w:ro -v "$R/tools":/tools:ro -v "$R/frozen":/out \
        --entrypoint python ytdigest-backend:latest -I /tools/acquire.py "$v" /out \
        >> "$R/logs/acquire.log" 2>> "$R/logs/acquire.err"; then
      got=1; echo "OK $v $(date +%T)" >> "$R/logs/acquire.log"
    else
      echo "FAIL $v $(date +%T) (next round backoff $backoff s)" >> "$R/logs/acquire.log"
    fi
    sleep 45
  done
  [ $missing -eq 0 ] && { echo "ALLDONE $(date +%T)" >> "$R/logs/acquire.log"; exit 0; }
  if [ $got -eq 1 ]; then backoff=120; else sleep $backoff; backoff=$(( backoff * 2 > 1800 ? 1800 : backoff * 2 )); fi
done
