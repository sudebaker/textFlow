import re

from entities_worker import entity_id  # module-level helper, not method


def test_entity_id_deterministic():
    eid = entity_id("PER", "María García")
    assert eid == entity_id("PER", "  María García  ")  # strips whitespace
    assert eid == entity_id("PER", "maría garcía")  # case-insensitive
    assert len(eid) == 12


def test_entity_id_different_label():
    assert entity_id("PER", "centro") != entity_id("ORG", "centro")


def test_entity_id_hex():
    assert re.fullmatch(r"[0-9a-f]{12}", entity_id("LOC", "Zaragoza"))


def test_entity_id_empty_strings():
    eid = entity_id("", "")
    assert re.fullmatch(r"[0-9a-f]{12}", eid)
