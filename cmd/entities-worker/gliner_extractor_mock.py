#!/usr/bin/env python3
"""
GLiNER Entity Extractor Script (Mock Version for Testing)

This script simulates GLiNER entity extraction for testing purposes.
Replace with real GLiNER when the package is properly installed.
"""

import argparse
import json
import sys
import random

def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Extract entities using GLiNER (mock)")
    parser.add_argument("--text", required=True, help="Text to extract entities from")
    parser.add_argument("--model-path", default="/models", help="Path to GLiNER model")
    parser.add_argument("--threshold", type=float, default=0.8, help="Confidence threshold")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size")
    parser.add_argument("--max-length", type=int, default=512, help="Maximum sequence length")
    parser.add_argument("--entity-types", default="PER,ORG,LOC,DATE,MONEY",
                       help="Comma-separated list of entity types")
    return parser.parse_args()

def extract_entities_mock(text: str, entity_types: list, threshold: float) -> list:
    """Mock entity extraction for testing."""
    # Mock entities based on common patterns in Spanish text
    mock_entities = []

    text_lower = text.lower()

    # Mock PER (Person) detection
    if "PER" in entity_types:
        person_patterns = ["juan pérez", "maría garcía", "antonio lópez", "carmen martín"]
        for person in person_patterns:
            if person in text_lower:
                start = text_lower.find(person)
                end = start + len(person)
                if random.random() > 0.3:  # 70% chance of detection
                    mock_entities.append({
                        "text": person.title(),
                        "label": "PER",
                        "confidence": round(random.uniform(0.8, 0.95), 2),
                        "start": start,
                        "end": end
                    })

    # Mock ORG (Organization) detection
    if "ORG" in entity_types:
        org_patterns = ["ministerio de hacienda", "ayuntamiento", "empresa", "universidad"]
        for org in org_patterns:
            if org in text_lower:
                start = text_lower.find(org)
                end = start + len(org)
                if random.random() > 0.4:  # 60% chance of detection
                    mock_entities.append({
                        "text": org.title(),
                        "label": "ORG",
                        "confidence": round(random.uniform(0.75, 0.92), 2),
                        "start": start,
                        "end": end
                    })

    # Mock LOC (Location) detection
    if "LOC" in entity_types:
        loc_patterns = ["madrid", "barcelona", "sevilla", "valencia"]
        for loc in loc_patterns:
            if loc in text_lower:
                start = text_lower.find(loc)
                end = start + len(loc)
                if random.random() > 0.5:  # 50% chance of detection
                    mock_entities.append({
                        "text": loc.title(),
                        "label": "LOC",
                        "confidence": round(random.uniform(0.7, 0.9), 2),
                        "start": start,
                        "end": end
                    })

    # Mock DATE detection
    if "DATE" in entity_types:
        date_patterns = ["2020", "2024", "enero", "diciembre"]
        for date in date_patterns:
            if date in text_lower:
                start = text_lower.find(date)
                end = start + len(date)
                if random.random() > 0.6:  # 40% chance of detection
                    mock_entities.append({
                        "text": date,
                        "label": "DATE",
                        "confidence": round(random.uniform(0.65, 0.88), 2),
                        "start": start,
                        "end": end
                    })

    return mock_entities

def main():
    """Main function."""
    try:
        # Parse arguments
        args = parse_arguments()

        # Parse entity types
        entity_types = [t.strip().upper() for t in args.entity_types.split(",")]

        # Mock entity extraction
        entities = extract_entities_mock(args.text, entity_types, args.threshold)

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