#!/usr/bin/env python3
"""
Download Whisper models for offline/air-gapped deployment.
Run this script on a machine with internet, then copy models to deployment.

Usage:
    python download_model.py --model small --output ./models_cache/whisper

Models are downloaded to: ~/.cache/whisper/ (HuggingFace cache format)
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


SUPPORTED_MODELS = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]

MODEL_SIZES = {
    "tiny": "~39MB",
    "base": "~74MB",
    "small": "~244MB",
    "medium": "~769MB",
    "large-v2": "~3GB",
    "large-v3": "~3GB",
}


def install_requirements():
    """Install faster-whisper if not present."""
    try:
        import faster_whisper
        print("faster-whisper already installed")
    except ImportError:
        print("Installing faster-whisper...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "faster-whisper", "--quiet"])
        print("faster-whisper installed successfully")


def download_model(model_size: str, cache_dir: str):
    """
    Download a faster-whisper model to the specified cache directory.
    
    faster-whisper downloads models from HuggingFace Hub automatically.
    We trigger the download by trying to load the model.
    """
    print(f"\n{'='*60}")
    print(f"Downloading Whisper model: {model_size}")
    print(f"Estimated size: {MODEL_SIZES.get(model_size, 'unknown')}")
    print(f"Cache directory: {cache_dir}")
    print(f"{'='*60}\n")
    
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["TRANSFORMERS_OFFLINE"] = "0"
    
    try:
        from faster_whisper import WhisperModel
        
        print(f"Downloading {model_size} model (this may take a while on first run)...")
        
        model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            download_root=cache_dir,
        )
        
        print(f"\n✓ Model '{model_size}' downloaded successfully!")
        print(f"  Location: {cache_dir}")
        
        model_path = Path(cache_dir)
        if model_path.exists():
            size = sum(f.stat().st_size for f in model_path.rglob('*') if f.is_file())
            print(f"  Size: {size / (1024**3):.2f} GB")
        
        return True
        
    except Exception as e:
        print(f"\n✗ Error downloading model: {e}")
        return False


def verify_model(model_size: str, cache_dir: str) -> bool:
    """Verify that a model is present and loadable."""
    try:
        from faster_whisper import WhisperModel
        
        print(f"\nVerifying model '{model_size}'...")
        model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            download_root=cache_dir,
        )
        
        print("✓ Model verification successful!")
        return True
    except Exception as e:
        print(f"✗ Model verification failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download Whisper models for offline deployment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Supported models:
  {', '.join(SUPPORTED_MODELS)}

Examples:
  # Download small model to default location
  python download_model.py --model small

  # Download small model to specific location  
  python download_model.py --model small --output /path/to/models

  # Download multiple models
  python download_model.py --model small
  python download_model.py --model large-v2
        """
    )
    
    parser.add_argument(
        "--model",
        "-m",
        choices=SUPPORTED_MODELS,
        default="small",
        help="Whisper model size to download (default: small)",
    )
    
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output directory for models (default: ~/.cache/whisper)",
    )
    
    parser.add_argument(
        "--verify",
        "-v",
        action="store_true",
        help="Verify model after download",
    )
    
    args = parser.parse_args()
    
    print(f"\n{'#'*60}")
    print(f"# Whisper Model Downloader (Offline Mode)")
    print(f"# Model: {args.model}")
    print(f"{'#'*60}")
    
    install_requirements()
    
    cache_dir = args.output or os.path.expanduser("~/.cache/whisper")
    os.makedirs(cache_dir, exist_ok=True)
    
    if download_model(args.model, cache_dir):
        if args.verify:
            verify_model(args.model, cache_dir)
        print(f"\n{'#'*60}")
        print(f"# Download complete!")
        print(f"#")
        print(f"# To use this model in deployment, mount the cache directory:")
        print(f"#   -v /path/to/models_cache/whisper:/root/.cache/whisper:ro")
        print(f"#")
        print(f"# Or copy to the target location:")
        print(f"#   cp -r {cache_dir} <deployment>/models_cache/whisper")
        print(f"{'#'*60}\n")
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
