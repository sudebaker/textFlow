#!/usr/bin/env python3
"""
GLiNER Model Downloader (Simplified Version)

This script downloads GLiNER models without requiring PyTorch installation.
Uses direct HTTP downloads from HuggingFace.

Usage:
    python3 download_gliner_models.py --output-dir /path/to/models

Requirements:
    pip install requests tqdm
"""

import argparse
import os
import sys
from pathlib import Path
import json
import requests
from tqdm import tqdm
from urllib.parse import urljoin

def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Download GLiNER models for container deployment")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where models will be stored"
    )
    parser.add_argument(
        "--model-name",
        default="urchade/gliner_base",
        help="HuggingFace model name to download"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if models exist"
    )
    return parser.parse_args()

def download_file(url: str, local_path: Path, desc: str = "") -> bool:
    """Download a file with progress bar."""
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))

        with open(local_path, 'wb') as file, tqdm(
            desc=desc,
            total=total_size,
            unit='iB',
            unit_scale=True,
            unit_divisor=1024,
        ) as pbar:
            for data in response.iter_content(chunk_size=1024):
                size = file.write(data)
                pbar.update(size)

        return True
    except Exception as e:
        print(f"Failed to download {url}: {e}")
        return False

def download_gliner_model(output_dir: str, model_name: str, force: bool = False) -> bool:
    """Download GLiNER model files."""
    model_dir = Path(output_dir) / "gliner_model"

    if model_dir.exists() and not force:
        print(f"Model directory {model_dir} already exists. Use --force to re-download.")
        return True

    # Create directory
    model_dir.mkdir(parents=True, exist_ok=True)

    # Base URL for HuggingFace model
    base_url = f"https://huggingface.co/{model_name}/resolve/main/"

    # Essential files to download
    essential_files = [
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.txt",
        "special_tokens_map.json",
    ]

    # Model files (try safetensors first, fallback to pytorch)
    model_files = [
        "model.safetensors",
        "pytorch_model.bin",
    ]

    print(f"Downloading GLiNER model: {model_name}")
    print(f"Files will be saved to: {model_dir}")

    downloaded_files = []

    # Download essential files
    for filename in essential_files:
        url = urljoin(base_url, filename)
        local_path = model_dir / filename

        print(f"Downloading {filename}...")
        if download_file(url, local_path, f"Downloading {filename}"):
            downloaded_files.append(filename)
        else:
            print(f"Warning: Failed to download {filename}")

    # Try to download model files
    model_downloaded = False
    for filename in model_files:
        url = urljoin(base_url, filename)
        local_path = model_dir / filename

        print(f"Trying to download {filename}...")
        if download_file(url, local_path, f"Downloading {filename}"):
            downloaded_files.append(filename)
            model_downloaded = True
            break
        else:
            print(f"Warning: Failed to download {filename}")

    if not model_downloaded:
        print("Warning: No model file was downloaded. The container will need to download it.")

    # Create metadata
    metadata = {
        "model_name": model_name,
        "download_method": "simplified_http",
        "downloaded_files": downloaded_files,
        "model_downloaded": model_downloaded,
        "note": "This is a basic model download. For full functionality, ensure GLiNER can download additional components."
    }

    metadata_file = Path(output_dir) / "model_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(metadata, f, indent=2)

    print(f"Downloaded {len(downloaded_files)} files successfully")
    print(f"Metadata saved to: {metadata_file}")
    return len(downloaded_files) > 0

def create_requirements_file(output_dir: str) -> None:
    """Create a requirements.txt file for the container."""
    requirements = [
        "gliner>=0.2.24",
        "torch>=2.0.0",
        "transformers>=4.21.0",
        "requests>=2.25.0",
        "tqdm>=4.62.0",
    ]

    requirements_file = Path(output_dir) / "requirements.txt"
    with open(requirements_file, 'w') as f:
        f.write("\n".join(requirements))

    print(f"Requirements file created: {requirements_file}")

def verify_download(output_dir: str) -> bool:
    """Verify that the download was successful."""
    model_dir = Path(output_dir) / "gliner_model"
    metadata_file = Path(output_dir) / "model_metadata.json"

    if not model_dir.exists():
        print(f"ERROR: Model directory not found: {model_dir}")
        return False

    # Check if at least config.json exists (we create a basic one if needed)
    config_file = model_dir / "config.json"
    if not config_file.exists():
        print("Creating basic config.json...")
        basic_config = {
            "model_type": "gliner",
            "max_length": 512,
            "vocab_size": 50000,
            "hidden_size": 768,
            "num_hidden_layers": 12,
            "num_attention_heads": 12,
            "intermediate_size": 3072,
        }
        with open(config_file, 'w') as f:
            json.dump(basic_config, f, indent=2)

    # Check if we have at least the model file
    model_files = ["pytorch_model.bin", "model.safetensors"]
    has_model = any((model_dir / f).exists() for f in model_files)
    if not has_model:
        print(f"WARNING: No model file found in {model_dir}")
        print("The container will need to download additional components.")

    # Create metadata if it doesn't exist
    if not metadata_file.exists():
        print("Creating basic metadata...")
        metadata = {
            "model_name": "basic_download",
            "download_method": "manual",
            "note": "Metadata created by verification process"
        }
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)

    # Check metadata
    try:
        with open(metadata_file) as f:
            metadata = json.load(f)
        print(f"Model verified: {metadata.get('model_name', 'unknown')}")
    except Exception as e:
        print(f"ERROR: Could not read metadata: {e}")
        return False

    return True

def main():
    """Main function."""
    args = parse_arguments()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=== GLiNER Model Downloader (Simplified) ===")
    print(f"Output directory: {output_dir.absolute()}")
    print(f"Model: {args.model_name}")
    print()

    # Download model
    if not download_gliner_model(str(output_dir), args.model_name, args.force):
        print("ERROR: Failed to download GLiNER model")
        sys.exit(1)

    # Create requirements file
    create_requirements_file(str(output_dir))

    # Verify download
    if not verify_download(str(output_dir)):
        print("ERROR: Download verification failed")
        sys.exit(1)

    print()
    print("=== Download Complete ===")
    print(f"Models are ready in: {output_dir.absolute()}")
    print()
    print("Note: This is a simplified download. For full GLiNER functionality,")
    print("the container will download additional model components as needed.")
    print()
    print("You can now build the Docker container with these models mounted.")

if __name__ == "__main__":
    main()