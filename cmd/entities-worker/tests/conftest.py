"""
Conftest for entities-worker tests.

Stubs out heavy runtime dependencies (pika, redis, prometheus_client, etc.)
so that worker.py can be imported without a live infrastructure.
"""

import sys
import types
import os
from pathlib import Path

# --- sys.path setup ---
WORKER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = WORKER_DIR.parent.parent
sys.path.insert(0, str(WORKER_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

# --- Stub heavy dependencies before any test imports worker ---

def _make_counter():
    label_stub = type("LabelStub", (), {"inc": lambda *a, **kw: None})()
    return type("Counter", (), {"labels": lambda *a, **kw: label_stub})()


def _make_histogram():
    return type("Histogram", (), {"observe": lambda *a, **kw: None})()


def _make_gauge():
    label_stub = type("LabelStub", (), {"set": lambda *a, **kw: None})()
    return type("Gauge", (), {"labels": lambda *a, **kw: label_stub})()


if "prometheus_client" not in sys.modules:
    pm = types.ModuleType("prometheus_client")
    pm.Counter = lambda *a, **kw: _make_counter()
    pm.Histogram = lambda *a, **kw: _make_histogram()
    pm.Gauge = lambda *a, **kw: _make_gauge()
    pm.start_http_server = lambda *a, **kw: None
    sys.modules["prometheus_client"] = pm

for _mod in ("pika", "redis", "requests"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

if "unidecode" not in sys.modules:
    ud = types.ModuleType("unidecode")
    ud.unidecode = lambda x: x
    sys.modules["unidecode"] = ud

if "rapidfuzz" not in sys.modules:
    rf = types.ModuleType("rapidfuzz")
    rf_fuzz = types.ModuleType("rapidfuzz.fuzz")
    rf_fuzz.ratio = lambda *a, **kw: 0.0
    sys.modules["rapidfuzz"] = rf
    sys.modules["rapidfuzz.fuzz"] = rf_fuzz
