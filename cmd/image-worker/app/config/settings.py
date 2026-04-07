from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    rabbitmq_url: str = "amqp://localhost:5672/"
    queue_name: str = "image"
    metrics_port: int = 8006
    prefetch_count: int = 2
    multimodal_llm_urls: str = "http://multimodal-llm:8000"
    multimodal_llm_timeout: int = 120
    multimodal_llm_max_retries: int = 3

    class Config:
        env_prefix = "IMAGE_"