#!/usr/bin/env python3
"""
Download ML models with proper HuggingFace cache structure for offline deployment.

This script downloads models using snapshot_download which creates the correct
cache structure that transformers expects in offline mode.

Usage:
    python download_models_offline.py

Models are saved to: <project-root>/models/huggingface_cache/
"""

import os
import subprocess
import sys
from pathlib import Path


MODEL_REQUIRED_FILE_GROUPS = {
    "urchade/gliner_small-v2.1": [
        ("config.json",),
        ("gliner_config.json",),
        ("tokenizer_config.json",),
        ("special_tokens_map.json",),
        ("spm.model", "tokenizer.json", "vocab.txt"),
        ("model.safetensors", "pytorch_model.bin"),
    ],
    "microsoft/deberta-v3-small": [
        ("config.json",),
        ("tokenizer_config.json",),
        ("special_tokens_map.json",),
        ("spm.model", "tokenizer.json", "vocab.txt"),
        ("model.safetensors", "pytorch_model.bin"),
    ],
    "BAAI/bge-m3": [
        ("config.json",),
        ("tokenizer_config.json",),
        ("modules.json",),
        ("spm.model", "tokenizer.json", "vocab.txt"),
        ("model.safetensors", "pytorch_model.bin"),
    ],
}

# Check dependencies
try:
    from huggingface_hub import snapshot_download
    from sentence_transformers import SentenceTransformer
except ImportError:
    print("❌ ERROR: Required packages not installed.")
    print("Please install: pip install huggingface_hub sentence-transformers")
    sys.exit(1)


def resolve_project_root() -> Path:
    """Resolve the project root from the current working directory."""
    current_dir = Path.cwd().resolve()

    for candidate in (current_dir, *current_dir.parents):
        if (candidate / "go.mod").exists() and (candidate / "cmd").is_dir():
            return candidate

    return Path(__file__).resolve().parents[2]


# Project root and cache directory
PROJECT_ROOT = resolve_project_root()
MODELS_DIR = PROJECT_ROOT / "models"
HF_CACHE_DIR = MODELS_DIR / "huggingface_cache"
DOCLING_MODELS_DIR = MODELS_DIR / "docling"

# Create directories
MODELS_DIR.mkdir(exist_ok=True, parents=True)
HF_CACHE_DIR.mkdir(exist_ok=True, parents=True)
DOCLING_MODELS_DIR.mkdir(exist_ok=True, parents=True)

print("=" * 80)
print("🚀 GLiNER Offline Model Downloader")
print("=" * 80)
print(f"Project root: {PROJECT_ROOT}")
print(f"Models dir: {MODELS_DIR}")
print(f"HF Cache dir: {HF_CACHE_DIR}")
print(f"Docling dir: {DOCLING_MODELS_DIR}")
print()

# Configure HuggingFace cache
os.environ["HF_HOME"] = str(HF_CACHE_DIR)


def _repo_to_model_cache_dir(repo_id: str, cache_dir: Path) -> Path:
    """Map repo_id to its HuggingFace hub cache directory."""
    repo_key = repo_id.replace("/", "--")
    return cache_dir / "hub" / f"models--{repo_key}"


def _is_snapshot_complete(snapshot_dir: Path, repo_id: str) -> tuple[bool, list[str]]:
    """Check whether a snapshot contains all required files for a repo."""
    missing_requirements = []
    required_groups = MODEL_REQUIRED_FILE_GROUPS.get(
        repo_id,
        [("config.json",), ("model.safetensors", "pytorch_model.bin")],
    )

    for group in required_groups:
        if not any((snapshot_dir / filename).exists() for filename in group):
            missing_requirements.append(" | ".join(group))

    return len(missing_requirements) == 0, missing_requirements


def _find_complete_snapshot(repo_id: str, cache_dir: Path) -> Path | None:
    """Return a complete local snapshot for the given model if available."""
    model_cache_dir = _repo_to_model_cache_dir(repo_id, cache_dir)
    snapshots_dir = model_cache_dir / "snapshots"

    if not snapshots_dir.exists() or not snapshots_dir.is_dir():
        return None

    snapshots = [p for p in snapshots_dir.iterdir() if p.is_dir()]
    if not snapshots:
        return None

    # Prefer newest snapshots first in case older ones are partial.
    snapshots.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for snapshot_dir in snapshots:
        is_complete, _ = _is_snapshot_complete(snapshot_dir, repo_id)
        if is_complete:
            return snapshot_dir

    return None


def download_model_with_cache(repo_id: str, cache_dir: Path, model_type: str = "model"):
    """
    Download model with proper HuggingFace cache structure.

    Args:
        repo_id: HuggingFace repo ID (e.g., "microsoft/deberta-v3-small")
        cache_dir: Base cache directory
        model_type: Type of model for logging

    Returns:
        Path to downloaded model
    """
    complete_snapshot = _find_complete_snapshot(repo_id, cache_dir)
    if complete_snapshot:
        print(f"⏭️  Skipping {model_type}: {repo_id}")
        print("   Reason: complete local snapshot already exists")
        print(f"   Location: {complete_snapshot}")
        print()
        return complete_snapshot

    print(f"📥 Downloading {model_type}: {repo_id}")
    print(f"   Cache: {cache_dir}")

    try:
        # snapshot_download creates the correct cache structure automatically
        # Structure: cache_dir/hub/models--org--name/snapshots/<hash>/
        local_path = snapshot_download(
            repo_id=repo_id,
            cache_dir=str(cache_dir / "hub"),
            local_files_only=False,  # First time needs internet
            ignore_patterns=["*.msgpack", "*.h5", "*.ot",
                             "*.tflite"],  # Skip unnecessary files
        )

        print(f"✅ {repo_id} downloaded successfully")
        print(f"   Location: {local_path}")

        # Validate downloaded snapshot completeness.
        is_complete, missing = _is_snapshot_complete(Path(local_path), repo_id)
        files = list(Path(local_path).glob("*"))
        print(f"   Files: {len(files)} files")

        if is_complete:
            print("   ✓ Snapshot integrity check passed")
        else:
            print("   ⚠ Snapshot integrity check failed")
            for requirement in missing:
                print(f"      Missing: {requirement}")
            return None

        print()
        return local_path

    except Exception as e:
        print(f"❌ Failed to download {repo_id}: {e}")
        import traceback
        traceback.print_exc()
        return None


def download_sentence_transformer(model_name: str, output_dir: Path):
    """
    Download sentence-transformer model (for embeddings worker).

    Args:
        model_name: Model name (e.g., "BAAI/bge-m3")
        output_dir: Output directory
    """
    print(f"📥 Downloading SentenceTransformer: {model_name}")
    print(f"   Output: {output_dir}")

    try:
        SentenceTransformer(model_name, cache_folder=str(output_dir))
        print(f"✅ {model_name} downloaded successfully")
        print(f"   Location: {output_dir / model_name.replace('/', '--')}")
        print()
        return True
    except Exception as e:
        print(f"❌ Failed to download {model_name}: {e}")
        return False


def verify_cache_structure(cache_dir: Path):
    """Verify that the cache has the correct structure."""
    print("🔍 Verifying cache structure...")

    hub_dir = cache_dir / "hub"
    if not hub_dir.exists():
        print("   ❌ Hub directory missing")
        return False

    print(f"   ✓ Hub directory: {hub_dir}")

    # List model directories
    model_dirs = list(hub_dir.glob("models--*"))
    print(f"   ✓ Found {len(model_dirs)} model(s)")

    for model_dir in model_dirs:
        model_name = model_dir.name.replace("models--", "").replace("--", "/")
        print(f"      • {model_name}")

        # Check snapshots
        snapshots_dir = model_dir / "snapshots"
        if snapshots_dir.exists():
            snapshots = list(snapshots_dir.glob("*"))
            print(f"         Snapshots: {len(snapshots)}")
        else:
            print("         ⚠ No snapshots directory")

    print()
    return True


def download_docling_models(output_dir: Path) -> bool:
    """Download Docling artifacts into a host directory by copying from Docker image cache."""
    print("📥 Downloading Docling models")
    print(f"   Output: {output_dir}")

    try:
        if output_dir.exists():
            existing_files = sum(
                1 for f in output_dir.rglob("*") if f.is_file())
            if existing_files > 0:
                print("⏭️  Skipping Docling models")
                print("   Reason: destination already contains files")
                print(f"   Files: {existing_files}")
                print()
                return True

        output_dir.mkdir(exist_ok=True, parents=True)

        # Use docker create + docker cp to avoid permission issues with volume mounts
        image = "quay.io/docling-project/docling-serve:latest"

        # Create a temporary container
        container_id = subprocess.check_output(
            ["docker", "create", image], text=True
        ).strip()

        try:
            # Copy models from container to host
            subprocess.run(
                [
                    "docker", "cp",
                    f"{container_id}:/opt/app-root/src/.cache/docling/models/.",
                    str(output_dir),
                ],
                check=True,
            )
        finally:
            # Clean up container
            subprocess.run(["docker", "rm", container_id], check=False)

        file_count = sum(1 for f in output_dir.rglob("*") if f.is_file())
        dir_count = sum(1 for d in output_dir.iterdir() if d.is_dir())
        print("✅ Docling models downloaded successfully")
        print(f"   Directories: {dir_count}")
        print(f"   Files: {file_count}")
        print()
        return True
    except FileNotFoundError:
        print("❌ Docker not found in PATH. Install Docker to download Docling models.")
        return False
    except subprocess.CalledProcessError as e:
        print(f"❌ Docling model download failed (exit code {e.returncode})")
        return False
    except Exception as e:
        print(f"❌ Unexpected error downloading Docling models: {e}")
        return False


def main():
    """Main download process."""

    # Models to download
    models = [
        {
            "repo_id": "urchade/gliner_small-v2.1",
            "type": "GLiNER (entities)",
            "critical": True,
        },
        {
            "repo_id": "microsoft/deberta-v3-small",
            "type": "DeBERTa (GLiNER backbone)",
            "critical": True,
        },
        {
            "repo_id": "BAAI/bge-m3",
            "type": "BGE-M3 (embeddings)",
            "critical": True,
        },
    ]

    print("🎯 Models to download:")
    for model in models:
        status = "CRITICAL" if model["critical"] else "OPTIONAL"
        print(f"   • {model['repo_id']} ({model['type']}) [{status}]")
    print()

    # Download each HuggingFace model
    successful = []
    failed = []

    for model in models:
        result = download_model_with_cache(
            model["repo_id"],
            HF_CACHE_DIR,
            model["type"]
        )

        if result:
            successful.append(model["repo_id"])
        else:
            failed.append(model["repo_id"])
            if model["critical"]:
                print(
                    f"⚠️  WARNING: Critical model {model['repo_id']} failed to download")

    # Download Docling artifacts (required by docling service in docker-compose)
    if download_docling_models(DOCLING_MODELS_DIR):
        successful.append("docling-models")
    else:
        failed.append("docling-models")
        print("⚠️  WARNING: Critical Docling artifacts failed to download")

    print("=" * 80)

    # Verify cache structure
    verify_cache_structure(HF_CACHE_DIR)

    # Calculate total size
    print("📊 Cache statistics:")
    total_size = sum(
        f.stat().st_size for f in HF_CACHE_DIR.rglob("*") if f.is_file())
    print(f"   Total size: {total_size / (1024**3):.2f} GB")
    print(f"   Location: {HF_CACHE_DIR}")
    print()

    # Summary
    print("=" * 80)
    print("📋 Summary:")
    total_targets = len(models) + 1  # +1 for docling-models
    print(f"   ✅ Successful: {len(successful)}/{total_targets}")
    for model_id in successful:
        print(f"      • {model_id}")

    if failed:
        print(f"   ❌ Failed: {len(failed)}/{total_targets}")
        for model_id in failed:
            print(f"      • {model_id}")
        print()
        print("⚠️  Some models failed to download. Check errors above.")
        return 1

    print()
    print("✨ All models downloaded successfully!")
    print()
    print("🐳 Next steps:")
    print("   1. Build Docker image: cd deploy/docker && docker compose build entities-worker")
    print("   2. Test offline: docker run --network=none entities-worker")
    print("   3. Deploy to production")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
