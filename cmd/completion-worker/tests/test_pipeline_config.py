"""Unit tests for PipelineDefinition (declarative DAG config)."""

import json

from pkg.worker_common.pipeline_config import PipelineDefinition


CONFIG = {
    "version": "v1",
    "default_pipeline": {
        "name": "full",
        "steps": ["extraction", "embeddings", "entities", "metadata"],
        "publish_queues": ["embeddings", "entities", "metadata"],
    },
    "pipelines": {
        "spreadsheet": {
            "name": "spreadsheet",
            "steps": ["extraction", "entities"],
            "publish_queues": ["entities"],
        }
    },
    "feature_extras": {"inferences": {"step": "inferences", "queue": "inferences"}},
    "rules": {"audio_replaces_extraction": True},
}


def test_queues_for_default():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=False, features=[]) == [
        "embeddings", "entities", "metadata",
    ]


def test_queues_for_spreadsheet():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=True, features=[]) == ["entities"]


def test_queues_for_inferences_feature():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=False, features=["inferences"]) == [
        "embeddings", "entities", "metadata", "inferences",
    ]


def test_steps_for_default():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=[]) == {
        "extraction", "embeddings", "entities", "metadata",
    }


def test_steps_for_spreadsheet():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=True, is_audio=False, features=[]) == {
        "extraction", "entities",
    }


def test_steps_for_audio_replaces_extraction():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=True, features=[]) == {
        "audio", "embeddings", "entities", "metadata",
    }


def test_steps_for_inferences_feature():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=["inferences"]) == {
        "extraction", "embeddings", "entities", "metadata", "inferences",
    }


def test_load_from_file(tmp_path):
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps(CONFIG), encoding="utf-8")
    pd = PipelineDefinition.load(str(p))
    assert pd.version == "v1"
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=[]) == {
        "extraction", "embeddings", "entities", "metadata",
    }
