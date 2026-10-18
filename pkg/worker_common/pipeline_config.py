"""PipelineDefinition loader for textFlow Python workers.

Reads configs/pipeline.json (JSON, no new dependencies) and exposes helpers
to derive routing queues (entry-stage workers) and required steps
(completion-worker) for a job.

Schema v2 is declarative: the file type selects the entry stage and the
processing profile composes the downstream stages (compose, never override).
Chunking is declared as an entry-stage capability — it runs inside the entry
stage and is not a queue, so no worker/queue is added for it.
"""

import json
import logging
import os
from typing import Dict, List, Optional, Set

DEFAULT_CONFIG_PATH = "/app/configs/pipeline.json"

#: Default processing profile (mirrors the orchestrator default).
DEFAULT_PROFILE = "full"

logger = logging.getLogger(__name__)


class PipelineDefinition:
    """Declarative DAG definition: types, profiles, feature extras and rules."""

    def __init__(self, data: Dict):
        self.data = data
        self.version = data.get("version", "v2")
        self.profiles = data.get("profiles", {})
        self.types = data.get("types", {})
        self.feature_extras = data.get("feature_extras", {})
        self.rules = data.get("rules", {})
        self.entries = sorted(
            {spec["entry"] for spec in self.types.values() if "entry" in spec}
        )

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

    def queues_for(
        self, *, entry: str, features: List[str], profile: str = DEFAULT_PROFILE
    ) -> List[str]:
        """Downstream routing queues for an entry-stage worker.

        Args:
            entry: entry stage that is publishing (extraction/audio/image).
            features: requested features (e.g. ["inferences"]).
            profile: processing profile (rag/full). Unknown values fall back
                to the default profile.

        Returns:
            Ordered list of target queues to publish the job to.
        """
        self._validate_entry(entry)
        queues: List[str] = []
        for stage in self._profile(profile).get("downstream", []):
            if stage != entry and stage not in queues:
                queues.append(stage)
        for feature in features:
            extra = self.feature_extras.get(feature)
            queue = extra.get("queue") if extra else None
            if queue and queue != entry and queue not in queues:
                queues.append(queue)
        return queues

    def steps_for(
        self, *, entry: str, features: List[str], profile: str = DEFAULT_PROFILE
    ) -> Set[str]:
        """Required completion steps for completion-worker.

        The entry stage (type-driven) plus the profile's downstream stages.
        Chunking is not a step: entry stages chunk internally.

        Args:
            entry: entry stage (extraction/audio/image).
            features: requested features (e.g. ["inferences"]).
            profile: processing profile (rag/full). Unknown values fall back
                to the default profile.

        Returns:
            Set of step names that must be completed before finalization.
        """
        self._validate_entry(entry)
        steps = {entry}
        steps.update(self._profile(profile).get("downstream", []))
        for feature in features:
            extra = self.feature_extras.get(feature)
            if extra and extra.get("step"):
                steps.add(extra["step"])
        return steps

    def has_capability(self, type_name: str, capability: str) -> bool:
        """Whether a declared file type advertises an entry-stage capability."""
        spec = self.types.get(type_name, {})
        return capability in spec.get("capabilities", [])

    def _validate_entry(self, entry: str) -> None:
        if self.entries and entry not in self.entries:
            raise ValueError(
                f"unknown entry stage {entry!r} (valid: {self.entries})"
            )

    def _profile(self, profile: str) -> Dict:
        """Resolve a processing profile, falling back to the default.

        Profiles (v2, schema in configs/pipeline.json):
        - rag:  entry + chunking + embeddings + inferences (no entities/metadata)
        - full: rag + entities + metadata (default; + image description)
        """
        if profile in self.profiles:
            return self.profiles[profile]
        if profile and profile != DEFAULT_PROFILE:
            logger.warning(
                "Unknown profile %r, falling back to %r", profile, DEFAULT_PROFILE
            )
        return self.profiles.get(DEFAULT_PROFILE, {})
