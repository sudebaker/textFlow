#!/usr/bin/env python3
"""Generate static OpenAPI docs for the GLiNER FastAPI service.

Writes `docs/swagger.json` and `docs/swagger.yaml` relative to this directory.
Defaults to mock mode to avoid loading a real model.
"""

import json
import os
import sys
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    print("pyyaml is required to generate swagger.yaml", file=sys.stderr)
    raise


ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def main() -> None:
    os.environ.setdefault("GLINER_USE_MOCK", "true")
    sys.path.insert(0, str(ROOT))

    from main import app  # local import after env is set

    schema = app.openapi()

    DOCS.mkdir(exist_ok=True)

    json_path = DOCS / "swagger.json"
    yaml_path = DOCS / "swagger.yaml"

    json_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    yaml_path.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")

    print(f"Wrote {json_path}")
    print(f"Wrote {yaml_path}")


if __name__ == "__main__":  # pragma: no cover
    main()
