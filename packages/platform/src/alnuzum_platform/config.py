"""Settings from the environment; secrets never get a default."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql+asyncpg://alnuzum:alnuzum-dev@localhost:55440/alnuzum"
    # "key_id:base64,key_id:base64": the first key encrypts, every key decrypts.
    secret_keys: SecretStr = SecretStr("")
    # Bearer token of the internal /connections API; empty keeps the API closed.
    admin_token: SecretStr = SecretStr("")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
