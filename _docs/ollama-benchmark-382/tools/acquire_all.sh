#!/bin/bash
# One video at a time, >=30 s apart, backoff 1/2/4 min on failure.
B=/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382
W=/home/hamlet/proj/simple_video_summariser/.claude/worktrees/agent-ac7302f7c04f8d55b
for v in "$@"; do
  [ -f "$B/frozen/$v.segments.json" ] && continue
  for delay in 0 60 120 240; do
    sleep $delay
    if docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$W":/w:ro -v "$B/tools":/tools:ro -v "$B/frozen":/out \
        --entrypoint python ytdigest-backend:latest -I /tools/acquire.py "$v" /out \
        >> "$B/logs/acquire.log" 2>> "$B/logs/acquire.err"; then break; fi
    echo "FAIL $v (delay was $delay)" >> "$B/logs/acquire.log"
  done
  sleep 30
done
echo ALLDONE >> "$B/logs/acquire.log"
