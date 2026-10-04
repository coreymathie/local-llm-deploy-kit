# Corey Mathie, 2026
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    OLLAMA_HOST: str = "http://127.0.0.1:11434"
    GATEWAY_HOST: str = "127.0.0.1"
    GATEWAY_PORT: int = 8080
    GATEWAY_DEFAULT_MODEL: str = "llama3.1:8b"
    GATEWAY_EMBED_MODEL: str = "nomic-embed-text"
    GATEWAY_MAX_UPLOAD_MB: int = 20
    GATEWAY_RATE_LIMIT_PER_MIN: int = 60
    GATEWAY_LOG_PROMPTS: bool = False
    GATEWAY_REDACT_PROMPTS: bool = False
    GATEWAY_DB_PATH: str = "./gateway.db"
    GATEWAY_LOG_DIR: str = "./logs"
    GATEWAY_ADMIN_BOOTSTRAP_KEY: str = ""


settings = Settings()
