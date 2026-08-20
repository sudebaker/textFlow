"""PipelineDefinition loader for textFlow Python workers.

Reads configs/pipeline.json (JSON, no new dependencies) and exposes helpers
to derive routing queues (extraction-worker) and required steps
(completion-worker) for a job.
"""

import json
import os
from typing import Dict, List, Optional, Set

DEFAULT_CONFIG_PATH = "/app/configs/pipeline.json"


class PipelineDefinition:
    """Declarative DAG definition: pipelines, feature extras and rules."""

    def __init__(self, data: Dict):
        self.data = data
        self.version = data.get("version", "v1")
        self.default_pipeline = data["default_pipeline"]
        self.pipelines = data.get("pipelines", {})
        self.feature_extras = data.get("feature_extras", {})
        self.rules = data.get("rules", {})

    @classmethod
    def load(cls, path: Optional[str] = None) -> "PipelineDefinition":
        """Load a PipelineDefinition from a JSON file.

        Args:
            path: JSON file path. Defaults to PIPELINE_CONFIG_PATH env or
                /app/configs/pipeline.json.

        Returns:
            Loaded PipelineDefinition.

        Raises:
            FileNotFoundError: if the config file does not exist.
        """
        config_path = path or os.getenv("PIPELINE_CONFIG_PATH", DEFAULT_CONFIG_PATH)
        with open(config_path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def queues_for(self, *, is_spreadsheet: bool, features: List[str]) -> List[str]:
        """Routing queues for extraction-worker.

        Args:
            is_spreadsheet: whether the document is a spreadsheet (entities-only).
            features: requested features (e.g. ["inferences"]).

        Returns:
            Ordered list of target queues to publish the job to.
        """
        base = (
            self.pipelines["spreadsheet"]["publish_queues"]
            if is_spreadsheet
            else self.default_pipeline["publish_queues"]
        )
        queues = list(base)
        for feature in features:
            extra = self.feature_extras.get(feature)
            if extra and extra.get("queue") and extra["queue"] not in queues:
                queues.append(extra["queue"])
        return queues

    def steps_for(
        self, *, is_spreadsheet: bool, is_audio: bool, features: List[str]
    ) -> Set[str]:
        """Required completion steps for completion-worker.

        Applies the audio_replaces_extraction rule and feature extra steps.

        Args:
            is_spreadsheet: whether the document is a spreadsheet.
            is_audio: whether the job produced an 'audio' step.
            features: requested features (e.g. ["inferences"]).

        Returns:
            Set of step names that must be completed before finalization.
        """
        base = list(self.default_pipeline["steps"])
        if is_spreadsheet:
            base = list(self.pipelines["spreadsheet"]["steps"])
        steps = set(base)
        if is_audio and self.rules.get("audio_replaces_extraction", True):
            steps.discard("extraction")
            steps.add("audio")
        for feature in features:
            extra = self.feature_extras.get(feature)
            if extra and extra.get("step"):
                steps.add(extra["step"])
        return steps