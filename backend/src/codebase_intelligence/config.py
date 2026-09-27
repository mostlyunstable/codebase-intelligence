from functools import lru_cache

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Values come from the environment; there are no secret defaults."""

    model_config = SettingsConfigDict(env_prefix="CI_", extra="ignore")

    # Must use the asyncpg driver, e.g. postgresql+asyncpg://user:pass@host:5432/db.
    # The role MUST NOT be a superuser or have BYPASSRLS, otherwise row-level security is inert.
    database_url: PostgresDsn


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # populated from the environment
