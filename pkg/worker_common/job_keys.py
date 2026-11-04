"""Redis keys scoped to a job, with the shared media TTL (D7).

Media keys (`:metadata:image`, `:metadata:audio`, `:audio_segments`) must
expire with the job: Redis runs `noeviction`, so an immortal media key leaks
memory. `set_job_key()` is the single writer for those keys.
"""

import os
from typing import Any, Optional

JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "86400"))


def job_key(job_id: str, suffix: str) -> str:
    """Build a job-scoped Redis key (``orchestrator:job:{id}:{suffix}``)."""
    return f"orchestrator:job:{job_id}:{suffix}"


def set_job_key(
    redis_client: Any,
    job_id: str,
    suffix: str,
    value: str,
    ttl: Optional[int] = None,
) -> None:
    """Write a job-scoped key with a TTL (``setex``), never an immortal key."""
    redis_client.setex(
        job_key(job_id, suffix),
        JOB_TTL_SECONDS if ttl is None else ttl,
        value,
    )
