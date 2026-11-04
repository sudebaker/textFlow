#!/usr/bin/env python3
"""Smoke test E2E de textFlow (stdlib, self-contained: dev y bundle air-gapped).

Flujo: PDF mínimo → POST /v1/documents/upload → poll → descarga resultados →
aserciones (status/steps del perfil, dims == EMBEDDINGS_DIMENSION, metadata,
texto). Consume la forma raw del /download (embedding_compressed = gzip+base64).

Uso: python3 deploy/scripts/smoke_test.py [--url http://localhost:8080]
        [--timeout 300] [--profile full|rag] [--job-id <uuid>]
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

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


# ----------------------------- PDF / HTTP -----------------------------------


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


def build_multipart(filename: str, data: bytes, mime: str) -> tuple[bytes, str]:
    boundary = "----smoke" + uuid.uuid4().hex[:12]
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode()
    body = head + data + f"\r\n--{boundary}--\r\n".encode()
    return body, boundary


def decode_embedding(raw) -> list[float]:
    """Forma raw: embedding_compressed = base64(gzip(float32 LE crudo)).

    Espejo exacto de decompressEmbeddings() en tools/client/main.go.
    Si ya es una lista (compresión desactivada) la devuelve tal cual.
    """
    if isinstance(raw, list):
        return raw
    blob = gzip.decompress(base64.b64decode(raw))
    count = len(blob) // 4
    if count * 4 != len(blob):
        raise SmokerFailure(f"embedding_compressed no múltiplo de 4 bytes ({len(blob)})")
    import struct
    return list(struct.unpack(f"<{count}f", blob))


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
        raise SmokerFailure("texto extraído vacío — extraction-worker")
    if "document_metadata" not in results:
        raise SmokerFailure("sin document_metadata — metadata incompleta")
    chunks = results.get("chunks") or []
    if not chunks:
        raise SmokerFailure("0 chunks — el texto no llegó a embeddings")
    if expected_dims:
        vec = chunks[0].get("embeddings") or chunks[0].get("embedding_compressed")
        if not vec:
            raise SmokerFailure("chunk sin embeddings — embeddings-worker/git")
        dim = len(decode_embedding(vec))
        raise_on_dims(dim, expected_dims)
    if "entities" in results and not results["entities"]:
        print("⚠️  entities vacío — GLiNER no detectó nada en el texto de prueba",
              file=sys.stderr)


def raise_on_dims(got: int, want: int) -> None:
    if got != want:
        raise SmokerFailure(
            f"embeddings dims {got} != {want} — revisa EMBEDDINGS_* y el backend")


# -------------------------------- main --------------------------------------


def http(url: str, data=None, headers: dict | None = None,
         timeout: int = 60) -> tuple[int, bytes]:
    headers = headers or {}
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers), timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upload(url: str, pdf: bytes) -> str:
    body, boundary = build_multipart("smoke.pdf", pdf, "application/pdf")
    status, resp = http(f"{url}/v1/documents/upload", data=body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    if status not in (200, 201, 202):
        raise SmokerFailure(f"upload HTTP {status}: {resp[:200].decode(errors='replace')}")
    js = json.loads(resp)
    job = js.get("job_id") or js.get("id")
    if not job:
        raise SmokerFailure(f"upload sin job_id: {resp[:200]}")
    return job


def poll(url: str, job: str, deadline: float, interval: float = 2.0) -> dict:
    while time.time() < deadline:
        status, resp = http(f"{url}/v1/documents/{job}", timeout=30)
        if status == 200:
            js = json.loads(resp)
            if js.get("status") in ("completed", "failed"):
                return js
        elif status not in (404, 503):
            print(f"  poll HTTP {status}", file=sys.stderr)
        time.sleep(interval)
    raise SmokerFailure(
        f"timeout esperando job {job} (sigue en el server: recupera con --job-id)")


def fetch_results(url: str, job: str) -> dict:
    status, data = http(f"{url}/v1/documents/{job}/download", timeout=120)
    if status != 200:
        raise SmokerFailure(f"download HTTP {status}")
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--profile", choices=("full", "rag"), default="full")
    ap.add_argument("--job-id", help="verificar un job ya existente")
    args = ap.parse_args(argv)

    dims_raw = _read_env("EMBEDDINGS_DIMENSION")
    expected_dims = int(dims_raw) if dims_raw.isdigit() else None

    try:
        if args.job_id:
            job = args.job_id
            print(f"→ verificando job existente: {job}")
        else:
            pdf = build_pdf(SMOKE_TEXT)
            print(f"→ upload {len(pdf)} bytes (profile={args.profile})")
            job = upload(args.url, pdf)
            print(f"  job= {job}")
        final = poll(args.url, job, time.time() + args.timeout)
        if final.get("status") != "completed":
            raise SmokerFailure(f"poll: status={final.get('status')} steps={final.get('steps')}")
        # status/steps en /v1/documents/{id}; datos en /download — fusionar
        results = {**fetch_results(args.url, job),
                   "status": final.get("status"), "steps": final.get("steps")}
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
