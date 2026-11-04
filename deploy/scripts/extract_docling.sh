#!/usr/bin/env bash
# Extrae los artefactos de modelado de docling-serve desde su imagen al
# staging models/docling (DOCLING_SERVE_ARTIFACTS_PATH=/models/docling).
# Requiere docker en host (la imagen se había descargado si no estaba).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAGING="$ROOT/models/docling"
IMAGE="${DOCLING_IMAGE:-quay.io/docling-project/docling-serve:latest}"
CONTAINER_SRC="/opt/app-root/src/.cache/docling/models/."

mkdir -p "$STAGING"

existing=$(find "$STAGING" -type f 2>/dev/null | wc -l)
if [ "$existing" -gt 50 ]; then
  echo "✓ docling: artefactos ya presentes en staging ($existing ficheros)"
  exit 0
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "→ docling: descargando imagen $IMAGE ..."
  docker pull "$IMAGE" >/dev/null
fi

cid="$(docker create "$IMAGE")"
trap 'docker rm -f "$cid" >/dev/null 2>&1' EXIT
docker cp "$cid:$CONTAINER_SRC" "$STAGING"/ >/dev/null

count=$(find "$STAGING" -type f | wc -l)
echo "✓ docling: $count ficheros de artefactos en $STAGING"
[ "$count" -gt 50 ]
