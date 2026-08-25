#!/usr/bin/env python3
"""Embeddings throughput benchmark (spec 2.1).

Sweeps batch sizes over [32, 64, 96, 128] against the real
EmbeddingService used by the embeddings-worker, reporting chunks/s,
tokens/s and latency percentiles per batch size.

Note: the production knob is EMBEDDING_BATCH_SIZE_GPU (singular), not
EMBEDDINGS_BATCH_SIZE (dead config).

Intended to run INSIDE the embeddings-worker container:

    docker compose -f deploy/docker/docker-compose.yml cp \
        scripts/bench/bench_embeddings.py textflow-embeddings-worker:/tmp/
    docker compose -f deploy/docker/docker-compose.yml exec \
        embeddings-worker python /tmp/bench_embeddings.py \
        --corpus /tmp/corpus_chunks.json

Corpus is produced by scripts/bench/prepare_corpus.sh.
"""

import argparse
import json
import os
import statistics
import sys
import time

WORKER_ROOT = os.getenv("EMBEDDINGS_WORKER_ROOT", "/app")
sys.path.insert(0, WORKER_ROOT)

BATCH_SIZES = [32, 64, 96, 128]


def approx_tokens(text: str) -> int:
    """Cheap token estimate; tiktoken when available, whitespace fallback."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text.split()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, help="corpus_chunks.json path")
    parser.add_argument(
        "--batch-sizes", type=int, nargs="+", default=BATCH_SIZES
    )
    parser.add_argument("--warmup-batches", type=int, default=2)
    args = parser.parse_args()

    from app.services.embeddings import EmbeddingService

    with open(args.corpus) as f:
        docs = json.load(f)

    texts = [
        c["text"]
        for d in docs
        for c in d.get("chunks", [])
        if c.get("text", "").strip()
    ]
    if not texts:
        print("ERROR: corpus has no non-empty chunks", file=sys.stderr)
        sys.exit(1)

    total_tokens = sum(approx_tokens(t) for t in texts)
    print(
        f"corpus: {len(docs)} docs, {len(texts)} chunks, "
        f"~{total_tokens} tokens"
    )

    device = os.getenv("EMBEDDINGS_DEVICE", "cuda:0")
    service = EmbeddingService(
        model_path=os.getenv("MODEL_PATH", "/models/bge-m3"),
        device=device,
    )
    if not service.load_model():
        print("ERROR: model failed to load", file=sys.stderr)
        sys.exit(1)

    results = []
    for bs in args.batch_sizes:
        batches = [texts[i : i + bs] for i in range(0, len(texts), bs)]
        # Warmup: first CUDA calls include kernel/alloc overhead.
        for batch in batches[: args.warmup_batches]:
            service.generate_embeddings(batch)

        latencies = []
        t0 = time.perf_counter()
        for batch in batches:
            b0 = time.perf_counter()
            service.generate_embeddings(batch)
            latencies.append(time.perf_counter() - b0)
        elapsed = time.perf_counter() - t0

        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
        row = {
            "batch_size": bs,
            "batches": len(batches),
            "elapsed_s": round(elapsed, 2),
            "chunks_per_s": round(len(texts) / elapsed, 1),
            "tokens_per_s": round(total_tokens / elapsed, 1),
            "batch_latency_p50_s": round(p50, 3),
            "batch_latency_p95_s": round(p95, 3),
        }
        results.append(row)
        print(json.dumps(row))

    print("\n| batch | chunks/s | tokens/s | p50 | p95 |")
    print("|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r['batch_size']} | {r['chunks_per_s']} | "
            f"{r['tokens_per_s']} | {r['batch_latency_p50_s']}s | "
            f"{r['batch_latency_p95_s']}s |"
        )

    out = os.path.join(os.path.dirname(args.corpus), "bench_embeddings.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
