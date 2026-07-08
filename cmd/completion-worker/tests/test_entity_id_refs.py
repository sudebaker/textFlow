import pytest
from pkg.worker_common.entity_utils import resolve_entity_refs


def test_resolve_exact_match():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9}
    }
    refs = ["textFlow"]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=0.85)
    assert resolved == ["abc123"]


def test_resolve_fuzzy_match():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9}
    }
    refs = ["TEXTFLOW"]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=0.85)
    assert resolved == ["abc123"]


def test_resolve_no_match_omits():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9}
    }
    refs = ["NonExistent"]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=0.85)
    assert resolved == []


def test_resolve_multiple_refs():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9},
        "def456": {"label": "LANG", "text": "Go", "confidence": 1.0}
    }
    refs = ["textFlow", "Go"]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=0.85)
    assert set(resolved) == {"abc123", "def456"}


def test_resolve_ignores_punctuation():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9},
        "def456": {"label": "LANG", "text": "Go", "confidence": 1.0},
    }
    refs = ["textFlow.", "Go,", "textFlow!!!"]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=0.85)
    assert sorted(resolved) == ["abc123", "def456"]


def test_resolve_returns_unique_ids():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9},
    }
    refs = ["textFlow", "TEXTFLOW", "textFlow."]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=0.85)
    assert resolved == ["abc123"]
