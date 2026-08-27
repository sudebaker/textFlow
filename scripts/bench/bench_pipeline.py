#!/usr/bin/env python3
"""Benchmark suite for the textFlow pipeline (spec 1.4).

Submits a corpus of documents through the running pipeline, then reads
Prometheus histograms to report P50/P95 of queue_time and job_duration per
worker/stage.

Usage:
    python scripts/bench/bench_pipeline.py [--docs a.pdf b.pdf ...]
    DOCS="a.pdf b.pdf" python scripts/bench/bench_pipeline.py

Requires: full stack up (make infra-up + workers), orchestrator on :8080,
and Prometheus on :9091 (deploy/docker/docker-compose.yml).
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

API_URL = os.getenv("API_URL", "http://localhost:8080")
PROM_URL = os.getenv("PROM_URL", "http://localhost:9091")
TIMEOUT_S = int(os.getenv("TIMEOUT_S", "600"))

DEFAULT_DOCS = [
    "corpus/Documento_9_R.pdf",
    "corpus/23F_9.pdf",
    "corpus/Documento_58_R.pdf",
    "corpus/27122023_RES.pdf",
    "corpus/Documento_1_R.pdf",
    "corpus/D.24.pdf",
    "corpus/Documento_42_R.pdf",
    "corpus/03_OP_ALESTE.pdf",
    "corpus/Sentencia-fiscal-general.pdf",
    "corpus/AASD_servodrivemanual.pdf",
]


def http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def submit_doc(path: str) -> str:
    """Upload a document and return its job_id."""
    import requests
    with open(path, "rb") as f:
        resp = requests.post(
            f"{API_URL}/v1/documents/upload",
            files={"file": f},
            timeout=120,
        )
    resp.raise_for_status()
    return resp.json().get("job_id", "")


def wait_job(job_id: str) -> str:
    """Poll job status until terminal, return final status."""
    waited = 0
    while waited < TIMEOUT_S:
        body = http_json(f"{API_URL}/v1/documents/{job_id}")
        status = body.get("status", "")
        if status in ("completed", "failed", "cancelled"):
            return status
        time.sleep(5)
        waited += 5
    return "timeout"


def prom_quantile(metric: str, quantile: float) -> float:
    """Query a Prometheus histogram_quantile for a metric name."""
    q = f'histogram_quantile({quantile}, sum(rate({metric}_bucket[5m])) by (le))'
    url = f"{PROM_URL}/api/v1/query?query={urllib.parse.quote(q)}"
    try:
        data = http_json(url)
        results = data.get("data", {}).get("result", [])
        if not results:
            return 0.0
        return float(results[0]["value"][1])
    except Exception:
        return 0.0


def main() -> None:
    import urllib.parse  # noqa: F401 (used in prom_quantile)

    parser = argparse.ArgumentParser(description="textFlow pipeline benchmark (spec 1.4)")
    parser.add_argument("--docs", nargs="*", default=None, help="Document paths (default: 10-doc corpus)")
    args = parser.parse_args()

    docs = args.docs or DEFAULT_DOCS
    docs = [d for d in docs if os.path.isfile(d)]
    if not docs:
        print("ERROR: no documents found", file=sys.stderr)
        sys.exit(1)

    print(f"== Benchmark: {len(docs)} documents ==")
    results = []
    for doc in docs:
        name = os.path.basename(doc)
        print(f"--- {name}")
        try:
            job_id = submit_doc(doc)
        except Exception as e:
            print(f"    ERROR submitting: {e}")
            continue
        print(f"    job={job_id}")
        status = wait_job(job_id)
        print(f"    status={status}")
        results.append({"doc": name, "job_id": job_id, "status": status})

    ok = [r for r in results if r["status"] == "completed"]
    print(f"\n== {len(ok)}/{len(results)} completed ==")

    # Report P50/P95 per worker for queue_time and job_duration.
    workers = ["extraction", "embeddings", "entities", "metadata", "inference", "image", "audio", "completion"]
    print("\n== Latency percentiles (P50/P95, seconds) ==")
    print(f"{'worker':<14} {'queue P50':>10} {'queue P95':>10} {'dur P50':>10} {'dur P95':>10}")
    for w in workers:
        q50 = prom_quantile(f"{w}_worker_queue_time_seconds", 0.50)
        q95 = prom_quantile(f"{w}_worker_queue_time_seconds", 0.95)
        d50 = prom_quantile(f"{w}_worker_job_duration_seconds", 0.50)
        d95 = prom_quantile(f"{w}_worker_job_duration_seconds", 0.95)
        print(f"{w:<14} {q50:>10.3f} {q95:>10.3f} {d50:>10.3f} {d95:>10.3f}")

    out = os.path.join(os.path.dirname(__file__), "results", "pipeline_bench.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"results": results, "docs": len(docs), "completed": len(ok)}, f, indent=1)
    print(f"\n== Results -> {out} ==")


if __name__ == "__main__":
    main()
