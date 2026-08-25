#!/usr/bin/env python3
"""GLiNER entity-extraction throughput benchmark (spec 2.2).

Sweeps GLINER_BATCH_SIZE over [16, 32, 64] against the same
GLiNER.from_pretrained loading path used by entities-worker, reporting
chunks/s and total entities found per batch size.

Intended to run INSIDE the entities-worker container:

    docker compose -f deploy/docker/docker-compose.yml cp \
        scripts/bench/bench_gliner.py textflow-entities-worker:/tmp/
    docker compose -f deploy/docker/docker-compose.yml exec \
        entities-worker python /tmp/bench_gliner.py \
        --corpus /tmp/corpus_chunks.json

Corpus is produced by scripts/bench/prepare_corpus.sh.
"""

import argparse
import json
import os
import statistics
import sys
import time

BATCH_SIZES = [16, 32, 64]
ENTITY_TYPES = os.getenv(
    "ENTITY_TYPES", "PERSON,ORGANIZATION,LOCATION,DATE,MONEY,EMAIL"
).split(",")


def load_gliner():
    from gliner import GLiNER

    model_path = os.getenv("GLINER_MODEL_PATH", "/models/gliner-small-v2.1")
    if not os.path.exists(
        os.path.join(model_path, "gliner_config.json")
    ):
        raise FileNotFoundError(f"gliner_config.json not found in {model_path}")
    print(f"Loading GLiNER from {model_path} ...", flush=True)
    model = GLiNER.from_pretrained(model_path, local_files_only=True)
    device = os.getenv("ENTITIES_DEVICE", "cuda")
    if device != "cpu":
        model = model.to(device)
        print(f"model moved to {device}", flush=True)
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=BATCH_SIZES)
    parser.add_argument("--threshold", type=float, default=0.4)
    parser.add_argument("--max-chunks", type=int, default=0,
                        help="cap chunks for quick runs (0 = all)")
    args = parser.parse_args()

    with open(args.corpus) as f:
        docs = json.load(f)

    texts = [
        c["text"]
        for d in docs
        for c in d.get("chunks", [])
        if c.get("text", "").strip()
    ]
    if args.max_chunks > 0:
        texts = texts[: args.max_chunks]
    if not texts:
        print("ERROR: corpus has no non-empty chunks", file=sys.stderr)
        sys.exit(1)

    print(f"corpus: {len(docs)} docs, {len(texts)} chunks")
    model = load_gliner()

    results = []
    for bs in args.batch_sizes:
        slices = [texts[i : i + bs] for i in range(0, len(texts), bs)]
        # Warmup on the first slice (CUDA kernels + tokenizer).
        if slices:
            model.predict_entities(
                slices[0], ENTITY_TYPES, threshold=args.threshold
            )

        latencies = []
        total_entities = 0
        t0 = time.perf_counter()
        for sl in slices:
            s0 = time.perf_counter()
            preds = model.predict_entities(
                sl, ENTITY_TYPES, threshold=args.threshold
            )
            latencies.append(time.perf_counter() - s0)
            total_entities += sum(len(p) for p in preds)
        elapsed = time.perf_counter() - t0

        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
        row = {
            "batch_size": bs,
            "slices": len(slices),
            "elapsed_s": round(elapsed, 2),
            "chunks_per_s": round(len(texts) / elapsed, 1),
            "entities_found": total_entities,
            "slice_latency_p50_s": round(p50, 3),
            "slice_latency_p95_s": round(p95, 3),
        }
        results.append(row)
        print(json.dumps(row))

    print("\n| batch | chunks/s | entities | p50 | p95 |")
    print("|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r['batch_size']} | {r['chunks_per_s']} | "
            f"{r['entities_found']} | {r['slice_latency_p50_s']}s | "
            f"{r['slice_latency_p95_s']}s |"
        )

    out = os.path.join(os.path.dirname(args.corpus), "bench_gliner.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
