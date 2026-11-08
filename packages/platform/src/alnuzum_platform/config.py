"""Settings from the environment; secrets never get a default."""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql+asyncpg://alnuzum:alnuzum-dev@localhost:55440/alnuzum"
    # "key_id:base64,key_id:base64": the first key encrypts, every key decrypts.
    secret_keys: SecretStr = SecretStr("")
    # Bearer token of the internal /connections API; shorter than 32 characters keeps the API closed.
    admin_token: SecretStr = SecretStr("")
    # Signs session cookies and CSRF tokens; shorter than 32 characters keeps the web closed.
    session_secret: SecretStr = SecretStr("")

    app_env: str = "dev"
    app_version: str = "dev"
    # where the browser reaches the platform; OAuth redirect addresses are built from it
    public_url: str = "http://127.0.0.1:8040"

    # OAuth clients (amoCRM needs none: its external integration sends keys per connection)
    kommo_client_id: str = ""
    kommo_client_secret: SecretStr = SecretStr("")
    zoho_client_id: str = ""
    zoho_client_secret: SecretStr = SecretStr("")

    # records written into one connected CRM per 24 hours
    quota_writes_per_day: int = 20
    # requests per minute from one IP address: all of them, and POSTs
    rate_per_minute: int = 120
    rate_post_per_minute: int = 20
    # how often every active connection is checked for access removed on the CRM side; 0 = never
    sweep_minutes: int = 30

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    def strong(self, value: SecretStr) -> bool:
        return len(value.get_secret_value()) >= MIN_SECRET


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
