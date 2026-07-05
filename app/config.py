from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    NET_BACKEND_URL: str = "http://localhost:5083"
    NET_BACKEND_TIMEOUT_SECONDS: float = 5.0
    GOOGLE_API_KEY: str = Field(
        ...,
        description="API key de Google AI Studio (Gemini).",
    )
    GOOGLE_MODEL: str = "gemini-2.5-flash"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
