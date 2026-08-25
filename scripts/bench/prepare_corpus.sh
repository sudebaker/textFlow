#!/bin/bash
# Prepare the benchmark corpus: submit representative documents through
# the running pipeline, then collect their chunk texts from Redis/artifact
# store into scripts/bench/corpus_chunks.json.
#
# Usage:
#   bash scripts/bench/prepare_corpus.sh            # default 10-doc corpus
#   DOCS="/a.pdf /b.pdf" bash scripts/bench/prepare_corpus.sh
#
# Requires: full stack up (make infra-up + workers) and orchestrator on :8080.

set -euo pipefail

API_URL="${API_URL:-http://localhost:8080}"
COMPOSE="docker compose -f deploy/docker/docker-compose.yml"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_JSON="${OUT_DIR}/corpus_chunks.json"
TIMEOUT_S="${TIMEOUT_S:-600}"

DOCS_DEFAULT=(
  "corpus/Documento_9_R.pdf"
  "corpus/23F_9.pdf"
  "corpus/Documento_58_R.pdf"
  "corpus/27122023_RES.pdf"
  "corpus/Documento_1_R.pdf"
  "corpus/D.24.pdf"
  "corpus/Documento_42_R.pdf"
  "corpus/03_OP_ALESTE.pdf"
  "corpus/Sentencia-fiscal-general.pdf"
  "corpus/AASD_servodrivemanual.pdf"
)
DOCS=(${DOCS:-${DOCS_DEFAULT[@]}})

echo "== Corpus: ${#DOCS[@]} documentos =="
declare -a RESULTS

for doc in "${DOCS[@]}"; do
  name=$(basename "$doc")
  if [ ! -f "$doc" ]; then
    echo "SKIP (no existe): $doc"
    continue
  fi
  echo "--- ${name}"
  resp=$(curl -s -m 120 -X POST "${API_URL}/v1/documents/upload" \
    -F "file=@${doc}")
  job_id=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null || true)
  if [ -z "$job_id" ]; then
    echo "ERROR subiendo ${name}: ${resp:0:200}"
    continue
  fi
  echo "    job=${job_id}"

  status=""
  waited=0
  while [ "$waited" -lt "$TIMEOUT_S" ]; do
    body=$(curl -s -m 10 "${API_URL}/v1/documents/${job_id}")
    status=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
    [ "$status" = "completed" ] || [ "$status" = "failed" ] && break
    sleep 5
    waited=$((waited + 5))
  done

  if [ "$status" != "completed" ]; then
    echo "ERROR: job ${job_id} terminó en '${status}'"
    continue
  fi

  ref=$(${COMPOSE} exec -T redis redis-cli --raw GET "orchestrator:job:${job_id}:chunks")
  hex=${ref#sha256:}
  path="/app/data/artifacts/${hex:0:2}/${hex:2:2}/${hex}.bin"
  chunks_json=$(${COMPOSE} exec -T embeddings-worker cat "$path")

  RESULTS+=("$(python3 -c "
import json,sys
chunks=json.loads(sys.argv[1])
print(json.dumps({'doc': sys.argv[2], 'job_id': sys.argv[3], 'chunks': chunks}))
" "$chunks_json" "$name" "$job_id")")
  echo "    ok: $(echo "$chunks_json" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))') chunks"
done

if [ "${#RESULTS[@]}" -eq 0 ]; then
  echo "ERROR: ningún documento se procesó correctamente" >&2
  exit 1
fi

python3 -c "
import json,sys
docs=[json.loads(r) for r in sys.argv[1:]]
json.dump(docs, open(sys.argv[-1],'w'), ensure_ascii=False, indent=1)
n=sum(len(d['chunks']) for d in docs)
print(f'\n== {len(docs)} docs, {n} chunks -> {sys.argv[-1]} ==')
" "${RESULTS[@]}" "$OUT_JSON"
