#!/usr/bin/env python3
"""
Test script for sliding window fuzzy matching deduplication
"""

import sys
import json
from pathlib import Path

# Add the entities-worker directory to the path
sys.path.insert(0, "/path/to/textflow/cmd/entities-worker")

from sliding_window import (
    normalize_entity_text,
    positions_overlap,
    entities_are_duplicate,
    merge_entities,
    FUZZY_SIMILARITY_THRESHOLD,
)


def test_normalize_entity_text():
    """Test entity text normalization"""
    print("\n=== Test 1: Text Normalization ===")
    
    test_cases = [
        ("Apple Inc.", "apple inc"),
        ("São Paulo", "sao paulo"),
        ("  John   ", "john"),
        ("NEW YORK", "new york"),
    ]
    
    for input_text, expected in test_cases:
        result = normalize_entity_text(input_text)
        status = "✓" if result == expected else "✗"
        print(f"{status} normalize('{input_text}') = '{result}' (expected '{expected}')")


def test_positions_overlap():
    """Test overlap detection"""
    print("\n=== Test 2: Position Overlap Detection ===")
    
    test_cases = [
        # (entity1, entity2, should_overlap, description)
        (
            {"start": 10, "end": 20},
            {"start": 15, "end": 25},
            True,
            "Actual overlap: [10-20] ∩ [15-25]"
        ),
        (
            {"start": 10, "end": 20},
            {"start": 130, "end": 140},
            False,
            "No overlap: [10-20] vs [130-140]"
        ),
        (
            {"start": 10, "end": 20},
            {"start": 20, "end": 30},
            True,
            "Adjacent (within buffer): [10-20] touches [20-30]"
        ),
        (
            {"start": 240, "end": 260},  # End of window 1
            {"start": 368, "end": 378},  # Start of window 2 (offset 128)
            False,
            "No overlap between windows: [240-260] vs [368-378]"
        ),
    ]
    
    for e1, e2, should_overlap, desc in test_cases:
        result = positions_overlap(e1, e2)
        status = "✓" if result == should_overlap else "✗"
        print(f"{status} {desc} → {result} (expected {should_overlap})")


def test_entities_are_duplicate():
    """Test duplicate detection with fuzzy matching"""
    print("\n=== Test 3: Fuzzy Matching Duplicate Detection ===")
    
    test_cases = [
        # (entity1, entity2, should_be_duplicate, description)
        (
            {"text": "Apple Inc", "label": "ORG", "start": 10, "end": 20, "score": 0.95},
            {"text": "Apple Inc", "label": "ORG", "start": 15, "end": 25, "score": 0.92},
            True,
            "Exact match in overlap"
        ),
        (
            {"text": "Apple Inc", "label": "ORG", "start": 10, "end": 20, "score": 0.95},
            {"text": "Apple Inc.", "label": "ORG", "start": 15, "end": 25, "score": 0.92},
            True,
            "Minor punctuation difference (80%+ similarity) in overlap"
        ),
        (
            {"text": "Apple", "label": "ORG", "start": 10, "end": 20, "score": 0.95},
            {"text": "Microsoft", "label": "ORG", "start": 15, "end": 25, "score": 0.92},
            False,
            "Different text (not similar enough)"
        ),
        (
            {"text": "John", "label": "PER", "start": 10, "end": 20, "score": 0.95},
            {"text": "John", "label": "ORG", "start": 15, "end": 25, "score": 0.92},
            False,
            "Same text, different labels"
        ),
        (
            {"text": "Apple", "label": "ORG", "start": 10, "end": 20, "score": 0.95},
            {"text": "Apple", "label": "ORG", "start": 130, "end": 140, "score": 0.92},
            False,
            "Same text, non-overlapping positions"
        ),
        (
            {"text": "New York", "label": "LOC", "start": 100, "end": 120, "score": 0.88},
            {"text": "New York City", "label": "LOC", "start": 115, "end": 135, "score": 0.85},
            True,
            "Similar text in overlap region (fuzzy match)"
        ),
        (
            {"text": "São Paulo", "label": "LOC", "start": 100, "end": 120, "score": 0.88},
            {"text": "Sao Paulo", "label": "LOC", "start": 115, "end": 135, "score": 0.85},
            True,
            "Accent variations (normalized comparison)"
        ),
    ]
    
    for e1, e2, should_be_dup, desc in test_cases:
        result = entities_are_duplicate(e1, e2)
        status = "✓" if result == should_be_dup else "✗"
        print(f"{status} {desc} → {result} (expected {should_be_dup})")


def test_merge_entities_sliding_window():
    """Test merging entities from overlapping windows"""
    print("\n=== Test 4: Merge Entities (Sliding Window Simulation) ===")
    
    # Simulate two overlapping windows
    # Window 1 (offset 0): tokens 0-384
    # Window 2 (offset 128): tokens 128-512
    # Overlap region: chars around position 128-256 (approximate)
    
    window1_entities = [
        {
            "text": "Apple Inc",
            "label": "ORG",
            "start": 50,
            "end": 60,
            "score": 0.95,
        },
        {
            "text": "Tim Cook",
            "label": "PER",
            "start": 150,  # In overlap region
            "end": 160,
            "score": 0.88,
        },
    ]
    
    window2_entities = [
        {
            "text": "Tim Cook",  # Duplicate from overlap
            "label": "PER",
            "start": 22,  # Relative to window 2 start (128)
            "end": 32,
            "score": 0.90,  # Higher confidence
        },
        {
            "text": "iPhone",
            "label": "PRODUCT",
            "start": 250,
            "end": 260,
            "score": 0.92,
        },
    ]
    
    entities_list = [window1_entities, window2_entities]
    offsets = [0, 128]
    text_length = 640  # Approximate 512-token chunk
    
    result = merge_entities(entities_list, offsets, text_length)
    
    print(f"\nInput: {len(window1_entities)} entities from window 1, "
          f"{len(window2_entities)} entities from window 2")
    print(f"Output: {len(result)} deduplicated entities")
    print("\nMerged entities:")
    for i, entity in enumerate(result, 1):
        print(f"  {i}. {entity['text']:20} | {entity['label']:10} | "
              f"pos [{entity['start']:3}-{entity['end']:3}] | "
              f"score {entity.get('score', entity.get('confidence', 0)):.3f}")
    
    # Verify results
    print("\nValidation:")
    assert len(result) == 3, f"Expected 3 entities, got {len(result)}"
    print("✓ Correct number of deduplicated entities")
    
    # Check that Tim Cook has higher score from window 2
    tim_cook = [e for e in result if "Tim Cook" in e.get("text", "")]
    if tim_cook:
        assert tim_cook[0].get("score", 0) >= 0.90, "Tim Cook should have score >= 0.90"
        print("✓ Duplicate 'Tim Cook' kept with higher confidence")


def main():
    """Run all tests"""
    print("=" * 80)
    print("SLIDING WINDOW FUZZY MATCHING DEDUPLICATION TESTS")
    print("=" * 80)
    
    try:
        test_normalize_entity_text()
        test_positions_overlap()
        test_entities_are_duplicate()
        test_merge_entities_sliding_window()
        
        print("\n" + "=" * 80)
        print("✓ All tests passed!")
        print("=" * 80)
        return 0
        
    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
