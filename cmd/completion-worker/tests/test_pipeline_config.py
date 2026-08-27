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
    "rules": {"audio_replaces_extraction": True, "image_replaces_extraction": True},
    "profiles": {
        "fast": {
            "steps": ["extraction", "metadata"],
            "publish_queues": ["metadata"],
        },
        "balanced": {
            "steps": ["extraction", "embeddings", "entities", "metadata"],
            "publish_queues": ["embeddings", "entities", "metadata"],
        },
        "full": {
            "steps": ["extraction", "embeddings", "entities", "metadata"],
            "publish_queues": ["embeddings", "entities", "metadata"],
        },
    },
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


def test_steps_for_image_replaces_extraction():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, is_image=True, features=[]) == {
        "image", "embeddings", "entities", "metadata",
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


def test_queues_for_fast_profile():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=False, features=[], profile="fast") == ["metadata"]


def test_queues_for_balanced_profile():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=False, features=[], profile="balanced") == [
        "embeddings", "entities", "metadata",
    ]


def test_queues_for_full_profile():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=False, features=[], profile="full") == [
        "embeddings", "entities", "metadata",
    ]


def test_queues_for_unknown_profile_falls_back_to_default():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=False, features=[], profile="unknown") == [
        "embeddings", "entities", "metadata",
    ]


def test_queues_for_fast_profile_with_inferences_feature():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=False, features=["inferences"], profile="fast") == [
        "metadata", "inferences",
    ]


def test_steps_for_fast_profile():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=[], profile="fast") == {
        "extraction", "metadata",
    }


def test_steps_for_balanced_profile():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=[], profile="balanced") == {
        "extraction", "embeddings", "entities", "metadata",
    }


def test_steps_for_full_profile():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=[], profile="full") == {
        "extraction", "embeddings", "entities", "metadata",
    }


def test_steps_for_unknown_profile_falls_back_to_default():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=[], profile="unknown") == {
        "extraction", "embeddings", "entities", "metadata",
    }


def test_steps_for_fast_profile_with_inferences_feature():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=["inferences"], profile="fast") == {
        "extraction", "metadata", "inferences",
    }


def test_steps_for_fast_profile_spreadsheet_uses_spreadsheet_pipeline():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=True, is_audio=False, features=[], profile="fast") == {
        "extraction", "entities",
    }
