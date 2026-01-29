#!/usr/bin/env python3
"""
GLiNER Entity Extractor Script

This script uses GLiNER to extract named entities from text.
It's designed to be called from the Go service.

Usage:
    python3 gliner_extractor.py --text "Your text here" --model-path /models

Arguments:
    --text: Input text to process
    --model-path: Path to GLiNER model directory
    --threshold: Confidence threshold (default: 0.8)
    --batch-size: Batch size for processing (default: 32)
    --max-length: Maximum sequence length (default: 512)
    --entity-types: Comma-separated list of entity types (default: PER,ORG,LOC,DATE,MONEY)
"""

import argparse
import json
import sys
import os
from typing import List, Dict, Any

try:
    from gliner import GLiNER
except ImportError:
    print(json.dumps({
        "success": False,
        "error": "GLiNER package not found. Install with: pip install gliner"
    }))
    sys.exit(1)

def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Extract entities using GLiNER")
    parser.add_argument("--text", required=True, help="Text to extract entities from")
    parser.add_argument("--model-path", default="/models", help="Path to GLiNER model")
    parser.add_argument("--threshold", type=float, default=0.8, help="Confidence threshold")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size")
    parser.add_argument("--max-length", type=int, default=512, help="Maximum sequence length")
    parser.add_argument("--entity-types", default="PER,ORG,LOC,DATE,MONEY", 
                       help="Comma-separated list of entity types")
    return parser.parse_args()

def load_model(model_path: str) -> GLiNER:
    """Load GLiNER model from local path."""
    try:
        if not os.path.exists(model_path):
            raise Exception(f"Model path does not exist: {model_path}")

        # Check if it's a valid model directory
        config_file = os.path.join(model_path, "config.json")
        if not os.path.exists(config_file):
            raise Exception(f"Invalid model directory: missing config.json in {model_path}")

        print(f"Loading GLiNER model from: {model_path}")
        model = GLiNER.from_pretrained(model_path)

        return model
    except Exception as e:
        raise Exception(f"Failed to load GLiNER model from {model_path}: {str(e)}")

def extract_entities(model: GLiNER, text: str, entity_types: List[str], 
                   threshold: float, max_length: int, batch_size: int) -> List[Dict[str, Any]]:
    """Extract entities from text using GLiNER."""
    try:
        # Extract entities
        entities = model.predict_entities(
            [text],
            entity_types,
            threshold=threshold,
            flat_ner=True
        )
        
        # Process results
        if len(entities) > 0 and len(entities[0]) > 0:
            result = []
            for entity in entities[0]:
                result.append({
                    "text": entity["text"],
                    "label": entity["label"],
                    "confidence": float(entity.get("score", threshold)),
                    "start": entity["start"],
                    "end": entity["end"]
                })
            return result
        else:
            return []
            
    except Exception as e:
        raise Exception(f"Entity extraction failed: {str(e)}")

def main():
    """Main function."""
    try:
        # Parse arguments
        args = parse_arguments()
        
        # Parse entity types
        entity_types = [t.strip() for t in args.entity_types.split(",")]
        
        # Load model (this might be slow, consider caching in production)
        model = load_model(args.model_path)
        
        # Extract entities
        entities = extract_entities(
            model,
            args.text,
            entity_types,
            args.threshold,
            args.max_length,
            args.batch_size
        )
        
        # Return successful response
        response = {
            "success": True,
            "entities": entities
        }
        print(json.dumps(response, ensure_ascii=False))
        
    except Exception as e:
        # Return error response
        response = {
            "success": False,
            "error": str(e)
        }
        print(json.dumps(response, ensure_ascii=False))
        sys.exit(1)

if __name__ == "__main__":
    main()