"""
Conftest for metadata-worker tests.

Mocks external dependencies so MetadataWorker can be imported without real connections.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

WORKER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = WORKER_DIR.parent.parent

_w = str(WORKER_DIR)
_r = str(PROJECT_ROOT)
for _p in (_w, _r):
    while _p in sys.path:
        sys.path.remove(_p)
sys.path.insert(0, _r)
sys.path.insert(0, _w)

sys.modules.pop("worker", None)

# Mock external libraries
sys.modules["pika"] = MagicMock()
sys.modules["redis"] = MagicMock()
sys.modules["requests"] = MagicMock()
sys.modules["prometheus_client"] = MagicMock()
sys.modules["fastapi"] = MagicMock()
sys.modules["fastapi.responses"] = MagicMock()

# Mock internal pkg modules
sys.modules["pkg.events_python"] = MagicMock()
sys.modules["pkg.logging_python"] = MagicMock()
