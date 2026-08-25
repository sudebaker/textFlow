"""Conftest: make the repo root importable so `pkg.*` resolves."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
root = str(PROJECT_ROOT)
if root not in sys.path:
    sys.path.insert(0, root)
