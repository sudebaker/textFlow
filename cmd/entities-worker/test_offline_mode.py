#!/usr/bin/env python3
"""
Test script to verify GLiNER model loading works for offline mode.
"""

import os
import sys

os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["ALLOW_REMOTE_DOWNLOAD"] = "true"
os.environ["GLINER_MODEL_NAME"] = "urchade/gliner_small-v2.1"

from gliner import GLiNER
from huggingface_hub import snapshot_download
from pathlib import Path

GLINER_MODEL_PATH = "/tmp/test_gliner_model"
GLINER_MODEL_NAME = os.getenv("GLINER_MODEL_NAME", "urchade/gliner_small-v2.1")
ALLOW_REMOTE_DOWNLOAD = True

model_path = Path(GLINER_MODEL_PATH)
cache_path = Path("/tmp/hf_cache") / "hub" / GLINER_MODEL_NAME.replace("/", "--")

print(f"🔍 GLiNER Model Path: {model_path}")
print(f"📦 HF Cache Path: {cache_path}")
print(f"🌐 Allow Remote Download: {ALLOW_REMOTE_DOWNLOAD}")

try:
    if model_path.exists() and any(model_path.iterdir()):
        print(f"🚀 Loading GLiNER from local path: {GLINER_MODEL_PATH}")

        model = GLiNER.from_pretrained(
            str(model_path),
            local_files_only=not ALLOW_REMOTE_DOWNLOAD,
        )
    elif ALLOW_REMOTE_DOWNLOAD:
        print(f"📥 Downloading GLiNER model: {GLINER_MODEL_NAME}")

        downloaded_path = snapshot_download(
            repo_id=GLINER_MODEL_NAME,
            repo_type="model",
            cache_dir=str(cache_path.parent),
        )

        print(f"💾 Downloaded to: {downloaded_path}")

        model = GLiNER.from_pretrained(
            downloaded_path,
            local_files_only=True,
        )
    else:
        raise FileNotFoundError(
            f"Model not found at {GLINER_MODEL_PATH} and remote download is disabled"
        )

    print(f"✅ GLiNER loaded successfully")
    print(f"   Model: {GLINER_MODEL_NAME}")
    print(f"   Type: {type(model).__name__}")

    text = "Cristiano Ronaldo plays for Al Nassr in Saudi Arabia."
    labels = ["person", "team", "country"]

    entities = model.predict_entities(text, labels)

    print(f"\n📝 Test prediction:")
    for entity in entities:
        print(
            f"   {entity['text']} => {entity['label']} (score: {entity['score']:.3f})"
        )

except Exception as e:
    print(f"❌ Model loading error: {e}")
    import traceback

    traceback.print_exc()
    sys.exit(1)
