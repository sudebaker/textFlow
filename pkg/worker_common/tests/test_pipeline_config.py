"""Unit tests for PipelineDefinition v2 (entry × profile composition).

The resolver is declarative: the file type selects the entry stage and the
profile composes the downstream stages (compose, never override). Chunking is
an entry-stage capability, not a queue.
"""

from pathlib import Path

import pytest

from pkg.worker_common.pipeline_config import PipelineDefinition

CONFIG = {
    "version": "v2",
    "profiles": {
        "rag": {"downstream": ["embeddings", "inferences"]},
        "full": {
            "downstream": ["embeddings", "entities", "metadata", "inferences"],
            "image_description": True,
        },
    },
    "types": {
        "document": {"entry": "extraction", "capabilities": ["chunking"]},
        "spreadsheet": {"entry": "extraction", "capabilities": ["chunking"]},
        "audio": {"entry": "audio", "capabilities": ["chunking"]},
        "image": {"entry": "image", "capabilities": ["chunking"]},
    },
    "feature_extras": {"inferences": {"step": "inferences", "queue": "inferences"}},
    "rules": {"audio_replaces_extraction": True, "image_replaces_extraction": True},
}

ENTRIES = ["extraction", "audio", "image"]

EXPECTED_DOWNSTREAM = {
    "rag": {"embeddings", "inferences"},
    "full": {"embeddings", "entities", "metadata", "inferences"},
}


@pytest.fixture()
def pd():
    return PipelineDefinition(CONFIG)


# --- type × profile composition matrix -------------------------------------

@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("profile", ["rag", "full"])
def test_steps_matrix(pd, entry, profile):
    assert pd.steps_for(entry=entry, features=[], profile=profile) == (
        {entry} | EXPECTED_DOWNSTREAM[profile]
    )


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("profile", ["rag", "full"])
def test_steps_never_contain_other_entries(pd, entry, profile):
    steps = pd.steps_for(entry=entry, features=[], profile=profile)
    assert steps & {"extraction", "audio", "image"} == {entry}


@pytest.mark.parametrize("profile,expected", [
    ("rag", ["embeddings", "inferences"]),
    ("full", ["embeddings", "entities", "metadata", "inferences"]),
])
def test_queues_matrix(pd, profile, expected):
    assert pd.queues_for(entry="extraction", features=[], profile=profile) == expected


@pytest.mark.parametrize("entry", ENTRIES)
def test_queues_never_contain_own_entry(pd, entry):
    assert entry not in pd.queues_for(entry=entry, features=[], profile="full")


def test_rag_excludes_entities_and_metadata(pd):
    steps = pd.steps_for(entry="extraction", features=[], profile="rag")
    assert "entities" not in steps
    assert "metadata" not in steps


@pytest.mark.parametrize("entry", ["extraction", "audio", "image"])
def test_rag_produces_inferences(pd, entry):
    assert "inferences" in pd.queues_for(entry=entry, features=[], profile="rag")


# --- spreadsheet no longer short-circuits ----------------------------------

def test_spreadsheet_honors_rag(pd):
    steps = pd.steps_for(entry="extraction", features=[], profile="rag")
    assert "entities" not in steps and "metadata" not in steps


def test_spreadsheet_honors_full(pd):
    steps = pd.steps_for(entry="extraction", features=[], profile="full")
    assert {"embeddings", "entities", "metadata", "inferences"} <= steps


# --- defaults, fallbacks, errors -------------------------------------------

def test_default_profile_is_full(pd):
    assert pd.steps_for(entry="extraction", features=[]) == pd.steps_for(
        entry="extraction", features=[], profile="full"
    )


def test_unknown_profile_falls_back_to_full(pd):
    assert pd.queues_for(entry="extraction", features=[], profile="balanced") == (
        pd.queues_for(entry="extraction", features=[], profile="full")
    )


def test_unknown_entry_is_rejected(pd):
    with pytest.raises(ValueError):
        pd.steps_for(entry="bogus", features=[], profile="rag")


def test_inferences_feature_does_not_duplicate_queue(pd):
    queues = pd.queues_for(entry="extraction", features=["inferences"], profile="full")
    assert queues.count("inferences") == 1


# --- declared capabilities --------------------------------------------------

def test_every_type_declares_chunking_and_a_valid_entry(pd):
    for type_name, spec in pd.types.items():
        assert "chunking" in spec["capabilities"], type_name
        assert spec["entry"] in ENTRIES, type_name


# --- loading the shipped repository config ---------------------------------

def test_repository_config_is_v2():
    config = Path(__file__).parents[3] / "configs/pipeline.json"
    pd = PipelineDefinition.load(str(config))
    assert pd.version == "v2"
    assert pd.steps_for(entry="extraction", features=[], profile="rag") == {
        "extraction", "embeddings", "inferences",
    }
    assert pd.steps_for(entry="image", features=[], profile="full") == {
        "image", "embeddings", "entities", "metadata", "inferences",
    }


def test_image_description_only_for_full(pd):
    """Description is a `full` capability; `rag` is OCR-only."""
    assert pd.wants_image_description("full") is True
    assert pd.wants_image_description("rag") is False
