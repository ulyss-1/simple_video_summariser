#!/bin/bash
# run.sh CMD...  : run CMD in the worktree with the benchmark environment
B=/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382
W=/home/hamlet/proj/simple_video_summariser/.claude/worktrees/agent-ac7302f7c04f8d55b
. "$B/env.sh"
export PYTHONPATH="$W"
export PATH="/home/hamlet/proj/simple_video_summariser/.venv/bin:$PATH"
cd "$W"
exec "$@"
