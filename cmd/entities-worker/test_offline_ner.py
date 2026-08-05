#!/usr/bin/env python3
"""
Test script for offline NER heuristic-based extraction.
"""

import sys
import os

sys.path.insert(0, "/app")

from entities_worker import EntitiesWorker


def test_offline_ner():
    worker = EntitiesWorker()
    worker.load_model()

    test_cases = [
        {
            "text": "Cristiano Ronaldo plays for Al Nassr in Saudi Arabia. He was paid $50 million in 2024.",
            "entity_types": ["PER", "ORG", "LOC", "DATE", "MONEY"],
            "expected": {
                "PER": 2,
                "ORG": 1,
                "LOC": 1,
                "DATE": 1,
                "MONEY": 1,
            },
        },
        {
            "text": "The meeting is scheduled for January 15, 2025 at the University of Madrid.",
            "entity_types": ["PER", "ORG", "LOC", "DATE", "MONEY"],
            "expected": {
                "PER": 0,
                "ORG": 1,
                "LOC": 1,
                "DATE": 1,
                "MONEY": 0,
            },
        },
        {
            "text": "Apple Inc. reported revenues of €100 billion in Q4 2024.",
            "entity_types": ["PER", "ORG", "LOC", "DATE", "MONEY"],
            "expected": {
                "PER": 0,
                "ORG": 1,
                "LOC": 0,
                "DATE": 1,
                "MONEY": 1,
            },
        },
    ]

    all_passed = True
    for i, test in enumerate(test_cases):
        print(f"\n--- Test {i + 1} ---")
        print(f"Text: {test['text']}")

        entities = worker.predict_entities(
            test["text"], test["entity_types"], threshold=0.5
        )

        print(f"\nExtracted entities ({len(entities)}):")
        for e in entities:
            print(f"  {e['text']} => {e['label']} (score: {e['confidence']:.3f})")

        counts = {}
        for e in entities:
            label = e["label"]
            counts[label] = counts.get(label, 0) + 1

        print(f"\nCounts: {counts}")
        print(f"Expected: {test['expected']}")

        for label, expected_count in test["expected"].items():
            actual_count = counts.get(label, 0)
            if actual_count >= expected_count:
                print(f"  ✅ {label}: {actual_count}/{expected_count}")
            else:
                print(f"  ❌ {label}: {actual_count}/{expected_count}")
                all_passed = False

    print("\n" + "=" * 50)
    if all_passed:
        print("✅ All tests passed!")
        return 0
    else:
        print("❌ Some tests failed!")
        return 1


if __name__ == "__main__":
    sys.exit(test_offline_ner())
