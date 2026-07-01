"""
Conftest for inference-worker tests.
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

sys.modules['pika'] = MagicMock()
sys.modules['redis'] = MagicMock()
sys.modules['requests'] = MagicMock()
sys.modules['pkg'] = MagicMock()
sys.modules['pkg.worker_common'] = MagicMock()
sys.modules['pkg.worker_common.base'] = MagicMock()
sys.modules['pkg.worker_common.rabbitmq'] = MagicMock()
