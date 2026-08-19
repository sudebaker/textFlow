"""Unit tests for entity_utils.deduplicate_entities and entity_id."""

from pkg.worker_common.entity_utils import deduplicate_entities, entity_id


def test_entity_id_deterministic():
    a = entity_id("PER", "María García")
    b = entity_id("PER", "  María García  ")
    c = entity_id("PER", "maría garcía")
    assert a == b == c
    assert len(a) == 12


def test_entity_id_unidecode():
    # unidecode aplicado: "á" == "a"
    assert entity_id("PER", "María") == entity_id("PER", "Maria")


def test_entity_id_different_label():
    assert entity_id("PER", "centro") != entity_id("ORG", "centro")


def test_dedup_empty_input():
    assert deduplicate_entities([]) == {}


def test_dedup_fallback_without_entity_id():
    entities = [
        {"label": "ORG", "text": "ACME", "confidence": 0.8},
        {"label": "ORG", "text": "ACME", "confidence": 0.9},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert len(eid) == 12
    assert result[eid]["confidence"] == 0.9  # highest wins


def test_dedup_identical_text_same_label_merges():
    entities = [
        {"label": "PER", "text": "María García", "confidence": 0.9, "entity_id": "aaa000000001"},
        {"label": "PER", "text": "María García", "confidence": 0.7, "entity_id": "aaa000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert result[eid]["confidence"] == 0.9


def test_dedup_accent_only_difference_merges():
    entities = [
        {"label": "ORG", "text": "Departamento de Educacion", "confidence": 0.8, "entity_id": "bbb000000001"},
        {"label": "ORG", "text": "Departamento de Educación", "confidence": 0.9, "entity_id": "bbb000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert result[eid]["confidence"] == 0.9


def test_dedup_different_text_stays_separate():
    entities = [
        {"label": "PER", "text": "María García", "confidence": 0.9, "entity_id": "ccc000000001"},
        {"label": "PER", "text": "Juan López", "confidence": 0.8, "entity_id": "ccc000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 2


def test_dedup_different_label_no_merge():
    entities = [
        {"label": "PER", "text": "Aragón", "confidence": 0.9, "entity_id": "ddd000000001"},
        {"label": "LOC", "text": "Aragón", "confidence": 0.8, "entity_id": "ddd000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 2


def test_dedup_offsets_preserved_from_highest_confidence():
    entities = [
        {
            "label": "PER", "text": "María", "confidence": 0.7,
            "entity_id": "eee000000001", "start": 10, "end": 15,
            "chunk_id": "chunk_001",
        },
        {
            "label": "PER", "text": "María", "confidence": 0.9,
            "entity_id": "eee000000002", "start": 100, "end": 105,
            "chunk_id": "chunk_002",
        },
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert result[eid]["start_offset"] == 100
    assert result[eid]["end_offset"] == 105
    assert result[eid]["chunk_id"] == "chunk_002"
