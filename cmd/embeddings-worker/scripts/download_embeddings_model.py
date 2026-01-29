#!/usr/bin/env python3
"""
BAAI/bge-m3 Model Downloader for Embeddings Service

Downloads the BAAI/bge-m3 multilingual embedding model for production deployment.
This model supports 100+ languages with 1024 dimensions.

Usage:
    python3 download_embeddings_model.py --output-dir /path/to/models

Requirements:
    pip install requests tqdm huggingface_hub
"""

import argparse
import os
import sys
from pathlib import Path
import json
import requests
from tqdm import tqdm

try:
    from huggingface_hub import snapshot_download, hf_hub_download
except ImportError:
    print("❌ huggingface_hub not found. Install with: pip install huggingface_hub")
    sys.exit(1)

def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Download BAAI/bge-m3 model for embeddings service")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where model will be stored"
    )
    parser.add_argument(
        "--model-name",
        default="BAAI/bge-m3",
        help="HuggingFace model name to download (default: BAAI/bge-m3)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if model exists"
    )
    return parser.parse_args()

def download_bge_model(output_dir: str, model_name: str, force: bool = False) -> bool:
    """Download BAAI/bge-m3 model using huggingface_hub."""
    model_dir = Path(output_dir) / "bge-m3_model"
    
    if model_dir.exists() and not force:
        print(f"Model directory {model_dir} already exists. Use --force to re-download.")
        return True
    
    try:
        print(f"Downloading model: {model_name}")
        print(f"Output directory: {model_dir}")
        
        # Download model using huggingface_hub
        snapshot_download(
            repo_id=model_name,
            local_dir=str(model_dir),
            local_dir_use_symlinks=False,
            force_download=force
        )
        
        # Verify key files exist
        required_files = [
            "config.json",
            "pytorch_model.bin",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.txt"
        ]
        
        missing_files = []
        for file in required_files:
            if not (model_dir / file).exists():
                missing_files.append(file)
        
        if missing_files:
            print(f"Warning: Missing files: {missing_files}")
        
        # Create metadata
        metadata = {
            "model_name": model_name,
            "download_date": str(Path.cwd()),
            "model_type": "sentence_transformer",
            "embedding_dimension": 1024,
            "max_sequence_length": 8192,
            "languages_supported": "100+ languages",
            "model_size_approx_gb": "2.2",
            "downloaded_files": [f.name for f in model_dir.rglob("*") if f.is_file()],
            "model_path": str(model_dir),
            "notes": "BAAI/bge-m3 multilingual embedding model"
        }
        
        metadata_file = Path(output_dir) / "model_metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        print(f"✅ Model downloaded successfully to: {model_dir}")
        print(f"📊 Embedding dimension: 1024")
        print(f"🌐 Multilingual support: 100+ languages")
        return True
        
    except Exception as e:
        print(f"❌ Failed to download model: {e}")
        return False

def create_requirements_file(output_dir: str) -> None:
    """Create requirements.txt for the embeddings service."""
    requirements = [
        "fastapi>=0.104.0",
        "uvicorn[standard]>=0.24.0",
        "sentence-transformers>=2.2.2",
        "torch>=2.0.0",
        "numpy>=1.24.0",
        "pydantic>=2.4.0",
        "python-multipart>=0.0.6",
        "safetensors>=0.3.1",
        "huggingface_hub>=0.17.0",
        "requests>=2.25.0",
        "tqdm>=4.62.0"
    ]
    
    requirements_file = Path(output_dir) / "requirements.txt"
    with open(requirements_file, 'w') as f:
        f.write("\n".join(requirements))
    
    print(f"📄 Requirements file created: {requirements_file}")

def verify_model(output_dir: str) -> bool:
    """Verify the model download was successful."""
    model_dir = Path(output_dir) / "bge-m3_model"
    metadata_file = Path(output_dir) / "model_metadata.json"
    
    if not model_dir.exists():
        print(f"❌ Model directory not found: {model_dir}")
        return False
    
    # Check for key model files
    key_files = [
        "config.json",
        "pytorch_model.bin",
        "tokenizer.json"
    ]
    
    missing_files = []
    for file in key_files:
        if not (model_dir / file).exists():
            missing_files.append(file)
    
    if missing_files:
        print(f"❌ Missing key files: {missing_files}")
        return False
    
    # Check metadata
    if not metadata_file.exists():
        print("❌ Metadata file not found")
        return False
    
    try:
        with open(metadata_file) as f:
            metadata = json.load(f)
        
        print(f"✅ Model verified: {metadata.get('model_name', 'unknown')}")
        print(f"📊 Embedding dimension: {metadata.get('embedding_dimension', 'unknown')}")
        return True
    except Exception as e:
        print(f"❌ Failed to verify metadata: {e}")
        return False

def main():
    """Main function."""
    args = parse_arguments()
    
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print("=== BAAI/bge-m3 Model Downloader ===")
    print(f"Output directory: {output_dir.absolute()}")
    print(f"Model: {args.model_name}")
    print()
    
    # Download model
    if not download_bge_model(str(output_dir), args.model_name, args.force):
        print("❌ Failed to download BAAI/bge-m3 model")
        sys.exit(1)
    
    # Create requirements file
    create_requirements_file(str(output_dir))
    
    # Verify download
    if not verify_model(str(output_dir)):
        print("❌ Model verification failed")
        sys.exit(1)
    
    print()
    print("=== Download Complete ===")
    print(f"📁 Models ready in: {output_dir.absolute()}")
    print("🚀 You can now build the Docker container")
    print()

if __name__ == "__main__":
    main()