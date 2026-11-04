import sys
from pathlib import Path

# path ABSOLUTO al módulo download_models (deploy/docker/): los inserts
# relativos a sys.path se resuelven distinto bajo pytest.
_HEREDIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_HEREDIR.parent))
