"""Media-key writes must carry the job TTL (D7, job-result-format)."""

from unittest.mock import MagicMock

from pkg.worker_common.job_keys import JOB_TTL_SECONDS, job_key, set_job_key


class TestJobKeys:
    def test_job_key_layout(self):
        assert job_key("job-1", "metadata:image") == (
            "orchestrator:job:job-1:metadata:image"
        )

    def test_default_ttl_is_24h(self):
        assert JOB_TTL_SECONDS == 86400

    def test_media_keys_written_with_ttl(self):
        redis_client = MagicMock()

        for suffix in ("metadata:image", "metadata:audio", "audio_segments"):
            set_job_key(redis_client, "job-1", suffix, "{}")
            redis_client.setex.assert_called_with(
                f"orchestrator:job:job-1:{suffix}", 86400, "{}"
            )

    def test_custom_ttl(self):
        redis_client = MagicMock()

        set_job_key(redis_client, "job-1", "metadata:image", "{}", ttl=60)

        redis_client.setex.assert_called_once_with(
            "orchestrator:job:job-1:metadata:image", 60, "{}"
        )
