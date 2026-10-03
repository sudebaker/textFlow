"""Per-page extraction provenance writer (spec §17: Redis, job-scoped, NO FSStore)."""

import json


def write_provenance(redis_client, job_id: str, report: dict) -> None:
    """orchestrator:job:{id}:extraction_provenance — control info, small, job-scoped.
    FSStore stays reserved for large blobs (spec §17)."""
    redis_client.set(
        f"orchestrator:job:{job_id}:extraction_provenance",
        json.dumps(report),
    )
