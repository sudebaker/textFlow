#!/usr/bin/env python3
"""
Download and prepare GLiNER backbone model (DeBERTa) for offline usage.

This script downloads the DeBERTa backbone that GLiNER requires,
so that the model can work in offline mode without internet access.

It performs two operations:
1. Downloads DeBERTa to the HuggingFace cache (for compatibility)
2. Copies DeBERTa files to /models/deberta-v3-large (for GLiNER offline access)

The second step is critical: GLiNER's config points to /models/deberta-v3-large,
so the tokenizer files must exist at that exact path in the container.
"""

import os
import sys
import shutil
import glob

# Allow online downloads during this setup script
os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["TRANSFORMERS_OFFLINE"] = "0"
os.environ["HF_HOME"] = "/home/app/.cache/huggingface"

from transformers import AutoTokenizer, AutoModel

def download_deberta_backbone():
    """Download the DeBERTa v3 Large backbone required by GLiNER"""
    
    model_id = "microsoft/deberta-v3-large"
    cache_dir = "/home/app/.cache/huggingface"
    
    print(f"📥 Downloading {model_id} to {cache_dir}...")
    print(f"   This may take 2-5 minutes...")
    
    try:
        # Download using transformers which creates the correct cache structure
        print(f"   Downloading tokenizer...")
        tokenizer = AutoTokenizer.from_pretrained(model_id, cache_dir=cache_dir)
        print(f"   ✓ Tokenizer downloaded")
        
        print(f"   Downloading model...")
        model = AutoModel.from_pretrained(model_id, cache_dir=cache_dir)
        print(f"   ✓ Model downloaded")
        
        print(f"✅ Successfully downloaded {model_id}")
        
        # Fix cache structure: remove .no_exist directories that confuse transformers
        fix_cache_structure(cache_dir, model_id)
        
        # Copy to flat directory for GLiNER offline access
        copy_to_flat_path(cache_dir, model_id)
        
        return True
        
    except Exception as e:
        print(f"❌ Failed to download: {e}")
        import traceback
        traceback.print_exc()
        return False


def fix_cache_structure(cache_dir, model_id):
    """Fix HuggingFace cache structure by removing .no_exist directories"""
    
    print(f"\n🔧 Fixing cache structure...")
    
    # Convert model_id to the cache directory format: microsoft/deberta-v3-large -> models--microsoft--deberta-v3-large
    model_safe_name = "models--" + model_id.replace("/", "--")
    model_cache_path = os.path.join(cache_dir, model_safe_name)
    
    if not os.path.exists(model_cache_path):
        print(f"   ⚠️  Cache path not found: {model_cache_path}")
        return
    
    print(f"   Cache path: {model_cache_path}")
    
    # Remove .no_exist directory if it exists
    no_exist_dir = os.path.join(model_cache_path, ".no_exist")
    if os.path.exists(no_exist_dir):
        print(f"   Removing .no_exist directory (contains incomplete downloads)...")
        try:
            shutil.rmtree(no_exist_dir)
            print(f"   ✓ .no_exist directory removed")
        except Exception as e:
            print(f"   ❌ Could not remove .no_exist: {e}")
            return
    else:
        print(f"   No .no_exist directory found (cache already clean)")
    
    # Verify snapshots directory exists with files
    snapshots_dir = os.path.join(model_cache_path, "snapshots")
    if os.path.exists(snapshots_dir):
        snapshot_subdirs = os.listdir(snapshots_dir)
        if snapshot_subdirs:
            snapshot_hash = snapshot_subdirs[0]
            snapshot_path = os.path.join(snapshots_dir, snapshot_hash)
            file_count = len(os.listdir(snapshot_path))
            print(f"   ✓ Found snapshots directory with {file_count} files")
    
    print(f"   ✓ Cache structure is now correct for offline use")


def copy_to_flat_path(cache_dir, model_id):
    """
    Copy DeBERTa tokenizer files from HuggingFace cache to a flat directory.
    
    GLiNER's config points to /models/deberta-v3-large and expects tokenizer files
    to be directly accessible there. HF cache uses hash-based snapshot structure,
    so we need to copy the files to a flat directory.
    """
    
    print(f"\n📂 Copying DeBERTa to flat path for offline access...")
    
    # Find the snapshot directory in HF cache
    model_safe_name = "models--" + model_id.replace("/", "--")
    model_cache_path = os.path.join(cache_dir, model_safe_name)
    snapshots_dir = os.path.join(model_cache_path, "snapshots")
    
    if not os.path.exists(snapshots_dir):
        print(f"   ⚠️  Snapshots directory not found: {snapshots_dir}")
        return False
    
    snapshot_subdirs = os.listdir(snapshots_dir)
    if not snapshot_subdirs:
        print(f"   ⚠️  No snapshots found in {snapshots_dir}")
        return False
    
    # Get the first (and usually only) snapshot
    snapshot_hash = snapshot_subdirs[0]
    source_path = os.path.join(snapshots_dir, snapshot_hash)
    target_path = "/models/deberta-v3-large"
    
    print(f"   Source: {source_path}")
    print(f"   Target: {target_path}")
    
    try:
        # Create target directory if it doesn't exist
        os.makedirs(target_path, exist_ok=True)
        
        # Copy all files from source to target
        # Use dirs_exist_ok=True to handle case where target already exists
        for item in os.listdir(source_path):
            src_item = os.path.join(source_path, item)
            dst_item = os.path.join(target_path, item)
            
            if os.path.isdir(src_item):
                # Recursively copy directories
                if os.path.exists(dst_item):
                    shutil.rmtree(dst_item)
                shutil.copytree(src_item, dst_item)
            else:
                # Copy files
                shutil.copy2(src_item, dst_item)
        
        # Verify critical files exist
        critical_files = [
            "tokenizer.json",
            "tokenizer_config.json",
            "config.json"
        ]
        
        missing_files = []
        for filename in critical_files:
            filepath = os.path.join(target_path, filename)
            if not os.path.exists(filepath):
                missing_files.append(filename)
        
        if missing_files:
            print(f"   ⚠️  Missing critical files: {missing_files}")
            return False
        
        file_count = len(os.listdir(target_path))
        print(f"   ✓ Successfully copied {file_count} files to {target_path}")
        print(f"   ✓ Critical tokenizer files verified")
        
        return True
        
    except Exception as e:
        print(f"   ❌ Failed to copy to flat path: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    if download_deberta_backbone():
        print("\n✅ DeBERTa backbone is ready for GLiNER offline mode")
        print("   - HuggingFace cache: /home/app/.cache/huggingface")
        print("   - Flat path: /models/deberta-v3-large")
        print("   - GLiNER can now load offline")
        sys.exit(0)
    else:
        print("\n❌ Failed to prepare DeBERTa backbone")
        sys.exit(1)

