"""
Conftest for audio-worker tests.

Sets sys.path so segment_chunker.py is imported from THIS worker directory
(cmd/audio-worker is not a valid Python package name because of the hyphen).
"""

import sys
from pathlib import Path

# --- sys.path setup ---
WORKER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = WORKER_DIR.parent.parent
_w = str(WORKER_DIR)
_r = str(PROJECT_ROOT)
for _p in (_w, _r):
    while _p in sys.path:
        sys.path.remove(_p)
sys.path.insert(0, _r)
sys.path.insert(0, _w)

# Evict stale modules so Python re-imports from the updated sys.path
sys.modules.pop("worker", None)
sys.modules.pop("segment_chunker", None)

# ---------------------------------------------------------------------------
# pytest hooks to ensure the worker dir stays importable when running tests
# from this directory (possibly alongside other worker test dirs).
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent


def pytest_pycollect_makemodule(module_path, parent):
    """Fire before each test module is imported; set our worker's paths."""
    if Path(module_path).resolve().parent == _TESTS_DIR:
        for _p in (_w, _r):
            while _p in sys.path:
                sys.path.remove(_p)
        sys.path.insert(0, _r)
        sys.path.insert(0, _w)
        sys.modules.pop("worker", None)
        sys.modules.pop("segment_chunker", None)
    return None
