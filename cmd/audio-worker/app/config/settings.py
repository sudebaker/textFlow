from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    rabbitmq_url: str = "amqp://localhost:5672/"
    queue_name: str = "audio"
    metrics_port: int = 8005
    max_audio_size_mb: int = 500
    prefetch_count: int = 2
    whisper_urls: str = "http://whisper:9666"
    whisper_timeout: int = 300
    whisper_max_retries: int = 3
    audio_chunk_max_chars: int = 1500

    class Config:
        env_prefix = "AUDIO_"