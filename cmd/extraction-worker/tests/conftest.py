"""Conftest for extraction-worker tests."""

import sys
from pathlib import Path

# Ensure the project root is importable so tests can `from pkg... import ...`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_root = str(PROJECT_ROOT)
while _root in sys.path:
    sys.path.remove(_root)
sys.path.insert(0, _root)