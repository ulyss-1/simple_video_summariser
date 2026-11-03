#!/bin/bash
# host_facts.sh OUTFILE : read-only facts about the host for the results document
{
echo "date: $(date -Is)"
echo "repo commit (worktree HEAD): $(git -C /home/hamlet/proj/simple_video_summariser/.claude/worktrees/agent-ac7302f7c04f8d55b rev-parse HEAD)"
echo "--- ollama --version"; OLLAMA_HOST=172.17.0.1:11434 ollama --version 2>&1
echo "--- ollama list"; OLLAMA_HOST=172.17.0.1:11434 ollama list
echo "--- systemctl show ollama -p Environment"; systemctl show ollama -p Environment
echo "--- docker image ls (ytdigest)"; docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep -i ytdigest
echo "--- docker volume ls"; docker volume ls --format '{{.Name}}'
echo "--- docker ps -a"; docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}'
echo "--- loadavg"; cat /proc/loadavg
echo "--- cpu"; lscpu | grep -i "model name\|^CPU(s)"
free -m
} > "$1" 2>&1
