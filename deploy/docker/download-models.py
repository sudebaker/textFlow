#!/usr/bin/env python3
"""
Descarga modelos ML una sola vez antes de docker build
Uso: python download-models.py

Los modelos se guardan en: <project-root>/models/
"""

import os
import sys
from pathlib import Path

# Asegurarse de que sentence-transformers y gliner estén instalados
try:
    from gliner import GLiNER
    from sentence_transformers import SentenceTransformer
except ImportError:
    print("Error: gliner or sentence-transformers not installed.")
    print("Please install them: pip install gliner sentence-transformers torch")
    sys.exit(1)


# Directorio de modelos (raíz del proyecto)
def resolve_project_root() -> Path:
    current_dir = Path.cwd().resolve()
    for candidate in (current_dir, *current_dir.parents):
        if (candidate / "go.mod").exists() and (candidate / "cmd").is_dir():
            return candidate
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = resolve_project_root()
models_dir = PROJECT_ROOT / "models"
models_dir.mkdir(exist_ok=True, parents=True)

print(f"Models directory: {models_dir}")

# Configurar HF_HOME para que los modelos se descarguen directamente en /models
os.environ["HF_HOME"] = str(models_dir)

# 1. Descargar GLiNER Small (modelo oficial del despliegue)
gliner_model_name = "urchade/gliner_small-v2.1"
gliner_local_path = models_dir / "gliner-small-v2.1"

if not gliner_local_path.exists():
    print(f"📥 Downloading GLiNER Small ({gliner_model_name})...")
    try:
        gliner = GLiNER.from_pretrained(gliner_model_name)
        gliner.save_pretrained(gliner_local_path)
        print(f"✅ GLiNER Small saved to {gliner_local_path}")
    except Exception as e:
        print(f"❌ Failed to download GLiNER Small: {e}")
        sys.exit(1)
else:
    print(f"☑️ GLiNER Small already exists at {gliner_local_path}")

# 2. Descargar BAAI/bge-m3
bge_model_name = "BAAI/bge-m3"
bge_local_path = models_dir / "bge-m3"

if not bge_local_path.exists():
    print(f"📥 Downloading {bge_model_name}...")
    try:
        embedder = SentenceTransformer(bge_model_name)
        embedder.save(str(bge_local_path))
        print(f"✅ {bge_model_name} saved to {bge_local_path}")
    except Exception as e:
        print(f"❌ Failed to download {bge_model_name}: {e}")
        sys.exit(1)
else:
    print(f"☑️ {bge_model_name} already exists at {bge_local_path}")

print("\n✨ All models downloaded successfully!")
total_size = sum(f.stat().st_size for f in models_dir.rglob("*") if f.is_file()) / (
    1024**3
)
print(f"Total size: ~{total_size:.2f} GB")
