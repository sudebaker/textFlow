#!/usr/bin/env python3
"""
Simple test for heuristic-based NER functions.
"""

import re
from typing import List, Dict


class SimpleNER:
    def __init__(self):
        self.ner_heuristics = {
            "DATE": self._extract_dates,
            "MONEY": self._extract_money,
            "ORG": self._extract_orgs,
            "LOC": self._extract_locs,
            "PER": self._extract_persons,
        }

    def _extract_dates(self, text: str) -> List[Dict]:
        dates = []
        patterns = [
            r"\d{1,2}/\d{1,2}/\d{2,4}",
            r"\d{1,2}-\d{1,2}-\d{2,4}",
            r"\d{4}-\d{2}-\d{2}",
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
            r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}",
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                dates.append(
                    {
                        "text": match.group(),
                        "label": "DATE",
                        "confidence": 0.75,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return dates

    def _extract_money(self, text: str) -> List[Dict]:
        money = []
        patterns = [
            r"\$\d+(?:,\d{3})*(?:\.\d{2})?",
            r"\d+(?:,\d{3})*(?:\.\d{2})?\s*(?:USD|EUR|GBP)",
            r"€\d+(?:,\d{3})*(?:\.\d{2})?",
            r"£\d+(?:,\d{3})*(?:\.\d{2})?",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                money.append(
                    {
                        "text": match.group(),
                        "label": "MONEY",
                        "confidence": 0.8,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return money

    def _extract_orgs(self, text: str) -> List[Dict]:
        orgs = []
        patterns = [
            r"(?:Inc\.|LLC|Corp\.|Ltd\.|S\.A\.|S\.L\.|B\.V\.|GmbH)",
            r"(?:University|Institute|Foundation|Association|Corporation)",
            r"(?:Bank|Insurance|Financial|Media|Tech|Software|Hardware)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                orgs.append(
                    {
                        "text": match.group(),
                        "label": "ORG",
                        "confidence": 0.6,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return orgs

    def _extract_locs(self, text: str) -> List[Dict]:
        locs = []
        patterns = [
            r"(?:New York|Los Angeles|Chicago|Houston|Phoenix|Philadelphia|San Antonio|San Diego)",
            r"(?:Madrid|Barcelona|Valencia|Sevilla|Málaga|Bilbao)",
            r"(?:Spain|France|Germany|Italy|United Kingdom|United States)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                locs.append(
                    {
                        "text": match.group(),
                        "label": "LOC",
                        "confidence": 0.65,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return locs

    def _extract_persons(self, text: str) -> List[Dict]:
        persons = []
        pattern = r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b"
        exclude = {
            "The",
            "This",
            "That",
            "What",
            "When",
            "Where",
            "Which",
            "Who",
            "How",
            "There",
        }
        for match in re.finditer(pattern, text):
            name = match.group()
            if name not in exclude:
                persons.append(
                    {
                        "text": name,
                        "label": "PER",
                        "confidence": 0.5,
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
        return persons

    def predict_entities(
        self, text: str, entity_types: List[str], threshold: float = 0.5
    ) -> List[Dict]:
        entities = []
        for entity_type in entity_types:
            if entity_type in self.ner_heuristics:
                extracted = self.ner_heuristics[entity_type](text)
                for ent in extracted:
                    if ent["confidence"] >= threshold:
                        entities.append(ent)
        return entities


def test_offline_ner():
    ner = SimpleNER()

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

        entities = ner.predict_entities(
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
    exit(test_offline_ner())
