import sys
from pathlib import Path

# path ABSOLUTO a deploy/scripts/ — los inserts relativos cambian bajo pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
