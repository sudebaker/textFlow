#!/usr/bin/env python3
"""Vision OCR benchmark tool (Fase F, spec §23-24).

Renders each page of each PDF in --corpus at each DPI in --dpi and POSTs it
to the vision-ocr service /transcribe. Writes ONE CSV row per (file, page,
dpi): latency_ms (SERVICE-reported elapsed_ms), chars (extracted_text length),
client-side wall-time, tokens if the backend returns usage (OpenAI-compat
/usage.prompt_tokens|completion_tokens — Ollama does, vLLM does), cached flag.
Ground truth / CER-WER: NOT computed here — see plan F.3 (subset with ground
truth later). This tool measures pipeline-side quality proxies + latency.

The service is internal (no published port by default). For a bench run from
the host, publish it: `docker compose run --rm -p 8080:8080 vision-ocr` with
dev LLM env (`-e VISION_LLM_BASE_URL=... -e VISION_LLM_MODEL=...` pointing at
Ollama), or run this tool INSIDE the backend network. URL override:
--url or env VISION_OCR_URL.
"""

import argparse
import csv
import glob
import os
import sys
import time
from datetime import datetime, timezone
from statistics import mean

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cmd", "extraction-worker"))
from vision.renderer import count_pages, render_page  # noqa: E402

HTTP_TIMEOUT = 300
CSV_COLUMNS = [
    "file", "page", "dpi", "latency_ms", "wall_ms", "chars", "words",
    "tokens_prompt", "tokens_completion", "cached", "error",
]



def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Benchmark vision-ocr across corpus/DPIs.")
    p.add_argument("--corpus", default="corpus/*.pdf",
                   help="glob pattern of PDFs to benchmark (default: corpus/*.pdf)")
    p.add_argument("--dpi", default="120,200,300",
                   help="comma-separated DPI list (default: 120,200,300)")
    p.add_argument("--url", default=os.environ.get("VISION_OCR_URL", "http://localhost:8080"),
                   help="vision-ocr base URL (default: $VISION_OCR_URL or http://localhost:8080)")
    p.add_argument("--out", default="bench_vision.csv",
                   help="output CSV path in cwd (default: bench_vision.csv; "
                        "despite plan F.2 mentioning docs/, writing into the repo is avoided)")
    p.add_argument("--limit-pages", type=int, default=0,
                   help="only benchmark the first N pages of each PDF (0 = all)")
    args = p.parse_args(argv)
    args.dpi_list = [int(d) for d in args.dpi.split(",")]
    return args


def build_row(file, page, dpi, response_json, error="", wall_ms=None):
    """One CSV row per (file, page, dpi). response_json=None on request failure."""
    row = {c: "" for c in CSV_COLUMNS}
    row.update(file=str(file), page=str(page), dpi=str(dpi),
               cached=str(False), wall_ms=("" if wall_ms is None else f"{wall_ms:.1f}"))
    if error:
        row["error"] = str(error)
        return row
    text = response_json.get("extracted_text")
    if not isinstance(text, str):
        row["error"] = f"missing extracted_text: {str(response_json)[:120]}"
        return row
    row["latency_ms"] = str(response_json.get("elapsed_ms", ""))
    row["chars"] = str(len(text))
    row["words"] = str(len(text.split()))
    usage = response_json.get("usage") or {}
    if isinstance(usage, dict):
        row["tokens_prompt"] = str(usage.get("prompt_tokens", "") or "")
        row["tokens_completion"] = str(usage.get("completion_tokens", "") or "")
    row["cached"] = str(bool(response_json.get("cached", False)))
    return row


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def transcribe(url, pdf_path, page, dpi, png):
    """Single-attempt POST. Returns (row, ok). Never retries; errors -> CSV row."""
    t0 = time.perf_counter()
    wall_ms = None
    try:
        r = requests.post(
            f"{url.rstrip('/')}/transcribe",
            files={"file": (f"page_{page}.png", png, "image/png")},
            data={"dpi": str(dpi)},
            timeout=HTTP_TIMEOUT,
        )
        wall_ms = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        return build_row(pdf_path, page, dpi, r.json(), wall_ms=wall_ms), True
    except Exception as e:
        if wall_ms is None:
            wall_ms = (time.perf_counter() - t0) * 1000
        return build_row(pdf_path, page, dpi, None, error=str(e)[:200], wall_ms=wall_ms), False


def main(argv=None):
    args = parse_args(argv)
    files = sorted(glob.glob(args.corpus))
    url = args.url
    print(f"# bench-vision-ocr run {datetime.now(timezone.utc).isoformat()}", file=sys.stderr)
    print(f"# url={url} corpus={args.corpus} ({len(files)} files) dpi={args.dpi_list} "
          f"limit_pages={args.limit_pages} out={args.out}", file=sys.stderr)
    if not files:
        print(f"ERROR: no files match '{args.corpus}'", file=sys.stderr)
        return 2
    t_start = time.perf_counter()
    rows = []
    for path in files:
        try:
            with open(path, "rb") as f:
                pdf_bytes = f.read()
            n_pages = count_pages(pdf_bytes)
        except Exception as e:
            rows.append(build_row(path, "", "", None, error=f"pdf: {e}"))
            print(f"[file] {path} UNREADABLE err={e}", file=sys.stderr)
            continue
        target = args.limit_pages if args.limit_pages > 0 else n_pages
        print(f"[file] {path} pages={min(target, n_pages)}", file=sys.stderr)
        for page in range(1, min(target, n_pages) + 1):
            try:
                png = render_page(pdf_bytes, page)
            except Exception as e:
                rows.append(build_row(path, page, "", None, error=f"render: {e}"))
                print(f"  page={page} dpi=- FAIL err=render: {e}", file=sys.stderr)
                continue
            for dpi in args.dpi_list:
                row, ok = transcribe(url, path, page, dpi, png)
                rows.append(row)
                if not ok:
                    print(f"  page={page} dpi={dpi} FAIL wall={row['wall_ms']}ms err={row['error']}", file=sys.stderr)
    write_csv(args.out, rows)
    _summary(rows)
    print(f"# done: {len(rows)} rows in {time.perf_counter() - t_start:.1f}s -> {args.out}", file=sys.stderr)
    return 0


def _summary(rows):
    """Per (file,dpi): mean latency over ok rows + total chars."""
    groups = {}
    for r in rows:
        if r.get("error"):
            continue
        groups.setdefault((r["file"], r["dpi"]), []).append(int(r["latency_ms"]))
    ok_count = len(rows) - sum(1 for r in rows if r.get("error"))
    fail_count = len(rows) - ok_count
    print(f"# summary: {ok_count} ok, {fail_count} failed (errors in CSV 'error' column)", file=sys.stderr)
    for (name, dpi), lats in sorted(groups.items()):
        print(f"  {name} dpi={dpi}: n={len(lats)} mean_latency={mean(lats):.0f}ms", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
