"""Centralized configuration via pydantic-settings.

All env vars are prefixed ``CC_`` so nothing collides with the host
environment. Settings are immutable after load; components receive the
resolved values through their constructors (constructor injection), which
keeps the modules testable without touching the environment.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CC_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    data_dir: Path = _REPO_ROOT / "data.parquet"
    cache_dir: Path = _REPO_ROOT / ".cache"
    economics_url: str = "http://localhost:8001/econ"

    openrouter_api_key: str | None = None
    llm_model: str = "openrouter/google/gemini-2.5-flash"
    llm_router_model: str = "openrouter/google/gemini-2.5-flash"

    max_query_rows: int = 1_000
    query_timeout_ms: int = 2_000
    economics_cache_ttl: float = 30.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
