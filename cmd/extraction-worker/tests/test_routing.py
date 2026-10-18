"""Regression tests for extraction routing vs PipelineDefinition (v2)."""

from pathlib import Path

from pkg.worker_common.pipeline_config import PipelineDefinition

_CONFIG = Path(__file__).parents[3] / "configs/pipeline.json"


def test_default_profile_routes_to_full_downstream():
    pd = PipelineDefinition.load(str(_CONFIG))
    assert pd.queues_for(entry="extraction", features=[]) == [
        "embeddings", "entities", "metadata", "inferences",
    ]


def test_rag_profile_routes_to_embeddings_and_inferences():
    pd = PipelineDefinition.load(str(_CONFIG))
    assert pd.queues_for(entry="extraction", features=[], profile="rag") == [
        "embeddings", "inferences",
    ]


def test_inferences_feature_does_not_duplicate_queue():
    pd = PipelineDefinition.load(str(_CONFIG))
    assert pd.queues_for(entry="extraction", features=["inferences"]) == [
        "embeddings", "entities", "metadata", "inferences",
    ]


def test_spreadsheet_uses_document_entry():
    pd = PipelineDefinition.load(str(_CONFIG))
    assert pd.queues_for(entry="extraction", features=[], profile="full") == [
        "embeddings", "entities", "metadata", "inferences",
    ]
