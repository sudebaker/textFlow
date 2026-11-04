#!/usr/bin/env python3
"""Smoke test E2E de textFlow: PDF mínimo → bin/client → aserciones.

El transporte lo hace ./bin/client (la forma canónica del consumidor:
descodifica embedding_compressed y fusiona features); este script orquesta
y aserta sobre su salida.

Uso: python3 deploy/scripts/smoke_test.py [--url http://localhost:8080]
        [--timeout 300] [--profile full|rag] [--job-id <uuid>]
Exit 0 = pipeline funcionando; 1 = fallo (dice qué comprobar).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLIENT_BIN = PROJECT_ROOT / "bin" / "client"

PROFILE_STEPS = {
    "full": ("extraction", "metadata", "embeddings", "entities", "inferences"),
    "rag": ("extraction", "embeddings", "inferences"),
}
SMOKE_TEXT = ("Informe trimestral de TextFlow. Angela Merkel visito Paris el "
              "pasado mes de marzo. El contrato se firmo el 15/04/2026 por "
              "2 millones de euros.")


class SmokerFailure(Exception):
    pass


def _read_env(name: str) -> str:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return ""
    for ln in env_file.read_text().splitlines():
        if ln.startswith(f"{name}="):
            return ln.split("=", 1)[1].strip().strip('"')
    return ""


# ----------------------------- PDF mínimo ----------------------------------


def build_pdf(text: str) -> bytes:
    """PDF 1-página válido (xref calculado) — docling lo procesa sin OCR."""
    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    buf = "%PDF-1.4\n"
    offsets: dict[int, int] = {}

    def add(n: int, body: str) -> None:
        nonlocal buf
        offsets[n] = len(buf)
        buf += f"{n} 0 obj\n{body}\nendobj\n"

    content = f"BT /F1 14 Tf 72 720 Td ({esc(text)}) Tj 0 -30 Td (segunda linea de prueba) Tj ET"
    add(1, "<< /Type /Catalog /Pages 2 0 R >>")
    add(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add(3, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           "/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
    add(4, f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    add(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    buf += "xref\n0 6\n0000000000 65535 f \n"
    for i in range(1, 6):
        buf += f"{offsets[i]:010d} 00000 n \n"
    starxref = len(buf)
    buf += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{starxref}\n%%EOF\n"
    return buf.encode("latin-1", "replace")


# ------------------------------ assertions ----------------------------------


def assert_results(results: dict, expected_dims: int | None,
                   required: tuple[str, ...]) -> None:
    """Asserts puros sobre el JSON fusionado (job state + resultados)."""
    if results.get("status") != "completed":
        raise SmokerFailure(
            f"job sin completar: status={results.get('status')} — "
            "docker logs <worker-que-cuelga> (ver steps abajo)")
    steps = results.get("steps") or {}
    for step in required:
        if steps.get(step) != "completed":
            raise SmokerFailure(
                f"step '{step}' != completed ({steps.get(step)}) — "
                f"docker logs textflow-{step}-worker")
    if not (results.get("text") or "").strip():
        raise SmokerFailure("texto extraído vacío — docker logs textflow-extraction-worker")
    if "document_metadata" not in results:
        raise SmokerFailure("sin document_metadata — pipeline de metadata incompleto")
    chunks = results.get("chunks") or []
    if not chunks:
        raise SmokerFailure("0 chunks — el texto no llegó a embeddings")
    if expected_dims:
        emb = chunks[0].get("embeddings") or []
        dim = len(emb[0]) if emb and isinstance(emb[0], list) else len(emb)
        raise_on_dims(dim, expected_dims)
    if not results.get("entities"):
        print("⚠️  entities vacío — GLiNER no detectó nada en el texto de prueba",
              file=sys.stderr)


def raise_on_dims(got: int, want: int) -> None:
    if got != want:
        raise SmokerFailure(
            f"embeddings dims {got} != {want} — revisa EMBEDDINGS_* y el backend")


def http_status_json(url: str, timeout: int = 30) -> dict:
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SmokerFailure(f"HTTP {e.code} en {url}") from e


def run_client(pdf_path: Path, out_path: Path, url: str, profile: str,
               timeout_s: int, job_id: str | None) -> str:
    """Ejecuta ./bin/client (build si falta) — devuelve job_id parseado."""
    if not CLIENT_BIN.is_file():
        subprocess.run(["make", "build-client"], cwd=PROJECT_ROOT,
                       capture_output=True, timeout=120, check=True)
    cmd = [str(CLIENT_BIN), "-i", str(pdf_path), "-o", str(out_path),
           "-u", url, "-p", profile, "--timeout", f"{timeout_s}s"] + (
           ["--job-id", job_id] if job_id else [])
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 60)
    out = cp.stdout + cp.stderr
    job = ""
    for ln in out.splitlines():
        if ln.startswith("Job created: "):
            job = ln.split("Job created: ", 1)[1].strip()
    if cp.returncode != 0:
        raise SmokerFailure(
            f"client exit={cp.returncode}:\n{(out.strip()[-400:])}")
    return job


# -------------------------------- main --------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--profile", choices=("full", "rag"), default="full")
    ap.add_argument("--job-id", help="verificar un job ya existente")
    args = ap.parse_args(argv)

    dims_raw = _read_env("EMBEDDINGS_DIMENSION")
    expected_dims = int(dims_raw) if dims_raw.isdigit() else None

    pdf_path = Path("/tmp/opencode/smoke.pdf")
    out_path = Path("/tmp/opencode/smoke_out.json")
    try:
        if not args.job_id:
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(build_pdf(SMOKE_TEXT))
            print(f"→ PDF {pdf_path.stat().st_size} bytes, client -p {args.profile}")
        job = run_client(pdf_path, out_path, args.url, args.profile,
                         args.timeout, args.job_id)
        if not job:
            raise SmokerFailure("client no reportó job id")
        job_state = http_status_json(f"{args.url}/v1/documents/{job}")
        results = {**json.loads(out_path.read_text()),
                   "status": job_state.get("status"), "steps": job_state.get("steps")}
        assert_results(results, expected_dims, PROFILE_STEPS[args.profile])
    except SmokerFailure as exc:
        print(f"❌ SMOKE FAIL: {exc}")
        return 1
    except (urllib.error.URLError, ConnectionError) as exc:
        print(f"❌ SMOKE FAIL (conectividad): {exc}")
        print("   ¿está el orchestrator arriba? make docker-up; curl :8080/health")
        return 1

    print("✅ SMOKE ok: upload → " + " → ".join(PROFILE_STEPS[args.profile]) + " → agregado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
