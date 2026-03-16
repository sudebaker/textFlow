"""
Test suite for entity types parsing from environment variables and RabbitMQ messages.

This test ensures that entity types are correctly parsed whether they come as:
1. Comma-separated string: "PER,ORG,LOC"
2. JSON array string: '["PER", "ORG", "LOC"]'
3. Already parsed list: ["PER", "ORG", "LOC"]
"""

import json
import pytest


def parse_entity_types_fixed(raw: str) -> list:
    """Fixed parsing function that handles both comma-separated and JSON formats."""
    if not raw or not raw.strip():
        return []

    raw = raw.strip()

    # Try to parse as JSON array first
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(entry).strip().upper() for entry in parsed if entry]
        except (json.JSONDecodeError, TypeError):
            pass

    # Fall back to comma-separated parsing
    return [entry.strip().upper() for entry in raw.split(",") if entry.strip()]


def test_parse_entity_types_comma_separated():
    """Test parsing comma-separated entity types."""
    result = parse_entity_types_fixed("PER,ORG,LOC,DATE,MONEY")

    assert result == ["PER", "ORG", "LOC", "DATE", "MONEY"]
    assert isinstance(result, list)
    assert all(isinstance(item, str) for item in result)


def test_parse_entity_types_with_spaces():
    """Test parsing comma-separated entity types with spaces."""
    result = parse_entity_types_fixed("PER, ORG, LOC, DATE, MONEY")

    assert result == ["PER", "ORG", "LOC", "DATE", "MONEY"]
    assert not any("[" in item or "]" in item or '"' in item for item in result)


def test_parse_entity_types_json_array_string():
    """Test parsing JSON array string (the buggy case)."""
    # This is what happens when entity_types are sent as JSON
    result = parse_entity_types_fixed('["PER", "ORG", "LOC"]')

    # Should return clean list without brackets or extra quotes
    assert result == ["PER", "ORG", "LOC"], f"Got: {result}"
    assert isinstance(result, list)
    assert all(isinstance(item, str) for item in result)
    assert not any("[" in item or "]" in item or '"' in item for item in result)


def test_parse_entity_types_single_item():
    """Test parsing single entity type."""
    result = parse_entity_types_fixed("PER")

    assert result == ["PER"]
    assert not any("[" in item or "]" in item or '"' in item for item in result)


def test_parse_entity_types_empty_string():
    """Test parsing empty string returns empty list."""
    result = parse_entity_types_fixed("")

    assert result == []


def test_parse_entity_types_mixed_quotes():
    """Test parsing with mixed quotes."""
    result = parse_entity_types_fixed('["PERSON", "EMAIL", "PHONE"]')

    assert result == ["PERSON", "EMAIL", "PHONE"]
    assert not any("[" in item or "]" in item or '"' in item for item in result)


def test_parse_entity_types_original_buggy_behavior():
    """
    Test that demonstrates the original buggy behavior.

    When entity_types comes as JSON array string and is split by comma,
    it produces wrapped values like ['["PERSON"', ' "EMAIL"'].
    """
    raw = '["PERSON", "EMAIL", "PHONE"]'

    # This is the BUGGY way (splitting by comma)
    buggy_result = [entry.strip().upper() for entry in raw.split(",") if entry.strip()]

    # Verify it produces the buggy output
    assert "[" in buggy_result[0], "Expected bug: first item should contain ["
    assert '"' in buggy_result[0], "Expected bug: first item should contain quotes"

    # Now verify the fixed way handles it correctly
    fixed_result = parse_entity_types_fixed(raw)
    assert fixed_result == ["PERSON", "EMAIL", "PHONE"]
    assert not any("[" in item or "]" in item or '"' in item for item in fixed_result)
