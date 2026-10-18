"""Integration tests for PipelineDefinition against the shipped pipeline.json (v2).

Unit coverage of the resolver matrix lives in
pkg/worker_common/tests/test_pipeline_config.py; these tests pin the contract
the completion-worker relies on against the real config file.
"""

from pathlib import Path

import pytest

from pkg.worker_common.pipeline_config import PipelineDefinition

_CONFIG = Path(__file__).parents[3] / "configs/pipeline.json"

FULL = {"embeddings", "entities", "metadata", "inferences"}
RAG = {"embeddings", "inferences"}


@pytest.fixture()
def pd():
    return PipelineDefinition.load(str(_CONFIG))


def test_shipped_config_is_v2(pd):
    assert pd.version == "v2"


@pytest.mark.parametrize("entry", ["extraction", "audio", "image"])
def test_full_profile_steps(pd, entry):
    assert pd.steps_for(entry=entry, features=[], profile="full") == {entry} | FULL


@pytest.mark.parametrize("entry", ["extraction", "audio", "image"])
def test_rag_profile_steps(pd, entry):
    assert pd.steps_for(entry=entry, features=[], profile="rag") == {entry} | RAG


def test_default_profile_is_full(pd):
    assert pd.steps_for(entry="extraction", features=[]) == {"extraction"} | FULL


def test_spreadsheet_no_longer_short_circuits(pd):
    assert pd.steps_for(entry="extraction", features=[], profile="rag") == {
        "extraction",
    } | RAG


def test_unknown_profile_falls_back_to_full(pd):
    assert pd.steps_for(entry="audio", features=[], profile="fast") == {
        "audio",
    } | FULL
