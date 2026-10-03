"""Golden regression test — VISION OFF must be byte-identical (plan Task 0.3).

Runs the extraction worker end-to-end against fakes (golden_utils) with a
deterministic 2-page PDF fixture and captures every Redis write and every
published queue message into tests/golden/baseline.json. Later phases must
produce EXACTLY this capture when VISION_OCR_ENABLED=false.

Regenerate the capture over PRISTINE code with:
    GOLDEN_REGENERATE=1 pytest cmd/extraction-worker/tests/test_golden_vision_off.py -v
Normal runs compare against the committed baseline.
"""

import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

# Third-party deps of worker.py are not all installed in the air-gapped test
# env. Stub them before importing so the module loads (pattern of
# test_metadata.py:16-27). pypdfium2/PIL are future vision-render deps
# (Phase B): stubbed up-front so the capture keeps working when worker starts
# importing the vision package with VISION disabled.
for _mod in (
    "aio_pika",
    "aio_pika.abc",
    "aiohttp",
    "langdetect",
    "magic",
    "redis",
    "textstat",
    "tiktoken",
    "prometheus_client",
    "pypdfium2",
    "PIL",
    "PIL.Image",
):
    sys.modules.setdefault(_mod, MagicMock())

import worker  # noqa: E402
from fixtures import build_pdf  # noqa: E402
from golden_utils import FakeExchange, FakeMsg, FakeRedis, FakeStore, canonical_json, normalize  # noqa: E402
from pkg.events_python import EventBus  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "baseline.json"
JOB_ID = "golden"

FIXTURE_PAGES = ["Hello world page one", "Second page text here"]

# Realistic 2-page docling response: several KB of Spanish notarial markdown
# (deterministic — the SourceClassifier regex and char chunking run over this).
_MD_PAGE_ONE = (
    "# ESCRITURA PÚBLICA DE COMPRAVENTA NÚMERO CIENTO CINCO\n\n"
    "## Comparecientes\n\n"
    "En la ciudad de nuestro notario, don Fernando Álvarez de Toledo, "
    "fedatario público del Ilustre Colegio, se hace constar que comparecen: "
    "don Juan Pérez Galdós, mayor de edad, vecino de esta ciudad, con domicilio "
    "en la calle Mayor número ocho, piso tercero, LN12345678 en el Documento "
    "Nacional de Identidad; y doña María Sotelongos Viuda de Depósito, mayor "
    "de edad, actuando en su propio nombre y derecho, en adelante cada una "
    "la parte y conjuntamente las partes.\n\n"
    "## Intervención y capacidad\n\n"
    "El notario fedatario certifica que les conozco y les tengo por personas "
    "capaces: les advierto de que les va a ser leída esta escritura pública, "
    "se les hacen las prevenciones del artículo ciento cuarenta y seis del "
    "Reglamento Notarial y toda vez que les parece, se hacen comparecer "
    "bajo la fe pública del notario actuario, con los mismos ratos de "
    "identidad consignados que anteceden, definidos en este acta notarial.\n\n"
    "## Objeto de la compraventa\n\n"
    "Se vende un inmueble rústico sito en el paraje denominado El Romeral, "
    "término municipal de Baeza, con superficie de tres hectáreas noventa "
    "áreas veinte centiáreas, referencia catastral 23026A0150013900001YH, "
    "con la finca registral número cuatro mil doscientos once de la "
    "Sección Primera del Registro de la Propiedad de Baeza.\n\n"
)
_MD_PAGE_TWO = (
    "## Precio y forma de pago\n\n"
    "El precio de la compraventa queda fijado en la cuantía total de "
    "ciento veinte mil euros (120.000 €), que recibe la parte vendedora "
    "a su entera satisfacción, y la restante mediante cheque bancario "
    "cruzado, tal como queda reflejado en el protocolo notarial del acta "
    "notarial en que la presente escritura pública se inscribe.\n\n"
    "## Cargas\n\n"
    "La finca se transmite libre de cargas, arrendatarios y gravámenes, "
    "estando al corriente de pago las cuotas del Impuesto de Bienes "
    "Inmuebles, así como los gastos de la Comunidad de Propietarios, "
    "según certificado expedido por el secretario de dicha finca.\n\n"
    "## Prevenciones finales\n\n"
    "En el supuesto de incumplimiento de lo estipulado en el plazo "
    "convenido, las partes se someten a los Juzgados y Tribunales del "
    "domicilio de la finca, con renuncia expresa a cualquier otro fuero "
    "que pudiera corresponderles. Protocolo: lotes 865.321 pedidos de "
    "copia autorizada han sido solicitados por la parte compradora.\n\n"
    "Y de conformidad con todo lo anterior, los comparecientes aceptan "
    "y firman la presente escritura pública. Do lo así otorgan y ante "
    "mí el notario fedatario certifica, de todo lo cual doy fe.\n\n"
)
DOCLING_RESPONSE = {
    "document": {
        "md_content": _MD_PAGE_ONE + _MD_PAGE_TWO,
        "text_content": None,
        "pages": [{"page_no": 1}, {"page_no": 2}],
    },
    "task_id": "golden-task",
    "task_status": "success",
}


def _fixed_metadata(pdf_bytes: bytes) -> dict:
    """Deterministic replacement for extract_document_metadata.

    Every key from test_metadata.py EXPECTED_KEYS, values fixed by the
    fixture bytes (real langdetect/exiftool would break json.dumps here:
    those modules are MagicMock stubs in this env).
    """
    return {
        "filename": "document.pdf",
        "file_size_bytes": len(pdf_bytes),
        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "author": None,
        "title": None,
        "subject": None,
        "creator": None,
        "producer": None,
        "creation_date": None,
        "modification_date": None,
        "page_count": 2,
        "encrypted": False,
        "mime_type": "application/pdf",
        "exif_data": {},
    }


class _IncomingMessage:
    """Minimal aio_pika.abc.AbstractIncomingMessage stand-in: body + process()."""

    def __init__(self, body: bytes):
        self.body = body

    @staticmethod
    def _noop_cm():
        class _CM:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return _CM()

    def process(self, requeue: bool = False):
        return self._noop_cm()


class _Channel:
    """Minimal aio_pika channel: only default_exchange is used by the worker."""

    def __init__(self):
        self.default_exchange = FakeExchange()


async def _fake_docling(self, document_bytes, filename):
    return DOCLING_RESPONSE


async def _fake_metadata(*args, **kwargs):
    pdf_bytes = base64.b64decode(_job_body()["document_base64"])
    return _fixed_metadata(pdf_bytes)


def _job_body():
    pdf = build_pdf(FIXTURE_PAGES)
    return {
        "job_id": JOB_ID,
        "document_base64": base64.b64encode(pdf).decode("ascii"),
    }


def _run_capture(monkeypatch):
    """Run one job end-to-end over fakes; return normalized capture."""
    # Determinism hooks BEFORE constructing the worker (plan Task 0.3).
    monkeypatch.setenv(
        "PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs" / "pipeline.json")
    )
    # langdetect/textstat are MagicMock stubs whose return values would break
    # json.dumps; replace analyze_text entirely with a deterministic dict.
    monkeypatch.setattr(worker, "analyze_text", lambda t: {"language": "es"})
    # chunk_text reads the module-global tokenizer at call time; None forces
    # the deterministic char-fallback chunker (tiktoken here is a stub).
    monkeypatch.setattr(worker, "tokenizer", None)
    monkeypatch.setattr(worker, "extract_document_metadata", _fake_metadata)
    # Belt-and-braces: guard any stray real-path call into exiftool/magic.
    monkeypatch.setattr(
        worker.subprocess, "run", MagicMock(returncode=0, stdout="[]")
    )
    monkeypatch.setattr(
        worker.magic, "from_buffer", lambda *a, **k: "application/pdf"
    )
    monkeypatch.setattr(
        worker.magic, "from_file", lambda *a, **k: "application/pdf"
    )
    monkeypatch.setattr(worker, "STORE", FakeStore())
    monkeypatch.setattr(worker.ExtractionWorker, "_docling_convert_async", _fake_docling)
    # aio_pika is a MagicMock module here; replace Message with FakeMsg so
    # published payloads are real bytes, with a deterministic PERSISTENT value.
    monkeypatch.setattr(
        worker,
        "aio_pika",
        SimpleNamespace(
            Message=FakeMsg,
            DeliveryMode=SimpleNamespace(PERSISTENT="PERSISTENT"),
        ),
    )

    w = worker.ExtractionWorker()
    fake_redis = FakeRedis()
    w.redis_client = fake_redis
    w.event_bus = EventBus(fake_redis)
    channel = _Channel()

    body = _job_body()
    message = _IncomingMessage(json.dumps(body).encode("utf-8"))
    asyncio.run(w._process_message_async(message, channel))

    return normalize(fake_redis.writes, channel.default_exchange.published)


def test_vision_off_golden(monkeypatch):
    """VISION off: Redis writes + published messages must equal the baseline."""
    capture = canonical_json(_run_capture(monkeypatch))

    if os.environ.get("GOLDEN_REGENERATE") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(capture, encoding="utf-8")
        return

    assert (
        GOLDEN_PATH.exists()
    ), f"missing {GOLDEN_PATH}; run with GOLDEN_REGENERATE=1 first"
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    if capture != expected:
        import difflib

        diff = "\n".join(
            list(
                difflib.unified_diff(
                    expected.splitlines(),
                    capture.splitlines(),
                    fromfile="baseline",
                    tofile="capture",
                    lineterm="",
                )
            )[:60]
        )
        raise AssertionError(
            "Output drifted from golden baseline (VISION must be off):\n" + diff
        )


def test_golden_capture_plumbing_sanity(monkeypatch):
    """Guard against broken fake plumbing (empty keys / MagicMock bodies)."""
    result = _run_capture(monkeypatch)
    writes = result["writes"]
    sets = {w["key"]: w["value"] for w in writes if w["op"] == "set"}

    assert sets.get(f"orchestrator:job:{JOB_ID}:text", "").startswith("sha256:")
    assert sets.get(f"orchestrator:job:{JOB_ID}:chunks", "").startswith("sha256:")
    assert f"orchestrator:job:{JOB_ID}:metadata:document" in sets

    published_keys = {p["routing_key"] for p in result["published"]}
    assert published_keys == {"embeddings", "entities", "metadata"}
    for p in result["published"]:
        assert isinstance(p["body"], dict)
        assert p["body"].get("queued_at") == "<ts>"
        assert p["body"].get("job_id") == JOB_ID
