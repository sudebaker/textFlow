"""Regression tests for extraction routing vs PipelineDefinition."""

from pathlib import Path

from pkg.worker_common.pipeline_config import PipelineDefinition

_CONFIG = Path(__file__).parents[3] / "configs/pipeline.json"


def test_spreadsheet_routes_to_entities_only():
    pd = PipelineDefinition.load(str(_CONFIG))
    assert pd.queues_for(is_spreadsheet=True, features=[]) == ["entities"]


def test_default_routes_to_three_stages():
    pd = PipelineDefinition.load(str(_CONFIG))
    assert pd.queues_for(is_spreadsheet=False, features=[]) == [
        "embeddings", "entities", "metadata",
    ]


def test_inferences_feature_appends_queue():
    pd = PipelineDefinition.load(str(_CONFIG))
    assert pd.queues_for(is_spreadsheet=False, features=["inferences"]) == [
        "embeddings", "entities", "metadata", "inferences",
    ]