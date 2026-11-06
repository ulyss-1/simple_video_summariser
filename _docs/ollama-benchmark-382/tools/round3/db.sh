#!/bin/bash
# Throwaway Postgres for round 3: network ytdigest-t382c-net, container ytdigest-t382c-db, 127.0.0.1:55434, no volume.
set -e
R=/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round3
W=/tmp/claude-1000/-home-hamlet-proj-simple-video-summariser/e3a54feb-0d54-45c2-8062-6e3778da6fb2/scratchpad/b382/round3/repo
if docker inspect ytdigest-t382c-db >/dev/null 2>&1; then echo DB_EXISTS; exit 0; fi
if [ ! -f $R/.dbpw ]; then (umask 077; head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n' > $R/.dbpw); fi
PW=$(cat $R/.dbpw)
docker network inspect ytdigest-t382c-net >/dev/null 2>&1 || docker network create ytdigest-t382c-net >/dev/null
docker run -d --name ytdigest-t382c-db --network ytdigest-t382c-net -p 127.0.0.1:55434:5432 \
  -e POSTGRES_PASSWORD="$PW" -e POSTGRES_USER=ytdigest -e POSTGRES_DB=ytdigest postgres:18-alpine >/dev/null
for i in $(seq 40); do docker exec ytdigest-t382c-db pg_isready -U ytdigest >/dev/null 2>&1 && break; sleep 1; done
(umask 077; cat > $R/env.sh <<EOT
# round 3: live analyzer settings with OLLAMA_NUM_CTX=16384; throwaway DB only
export DATABASE_URL='postgresql://ytdigest:${PW}@127.0.0.1:55434/ytdigest'
export OLLAMA_HOST="\${OLLAMA_HOST_OVERRIDE:-http://172.17.0.1:11434}"
export SUMMARIZER=ollama
export OLLAMA_NUM_CTX="\${OLLAMA_NUM_CTX:-16384}"
export OLLAMA_OUTPUT_RESERVE_TOKENS=2048
export OLLAMA_TIMEOUT_SEC=900
export CHUNK_SEC=900
export OVERLAP_SEC=60
export PROMPT_VERSION=v1
export LOG_FORMAT=json
EOT
)
cd $W
. $R/env.sh
export PATH=/home/hamlet/proj/simple_video_summariser/.venv/bin:$PATH
alembic upgrade head 2>&1 | tail -3
echo DB_READY
