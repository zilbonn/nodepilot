"""Runtime configuration for the NodePilot server."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables or a local `.env` file."""

    model_config = SettingsConfigDict(
        env_prefix="PROXMOX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    api_url: str = Field(default="https://127.0.0.1:8006/api2/json")
    user: str = Field(default="nodepilot@pve")
    token_name: str = Field(default="mcp")
    token_value: str = Field(default="")
    default_node: str = Field(default="localhost")
    verify_ssl: bool = Field(default=False)
    timeout: float = Field(default=30.0)

    @field_validator("api_url")
    @classmethod
    def normalize_api_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise ValueError("PROXMOX_API_URL must not be empty")
        if not value.endswith("/api2/json"):
            value = f"{value}/api2/json"
        return value

    @property
    def token_id(self) -> str:
        return f"{self.user}!{self.token_name}"

    @property
    def authorization_header(self) -> str:
        if not self.token_value:
            raise ValueError("PROXMOX_TOKEN_VALUE is required")
        return f"PVEAPIToken={self.token_id}={self.token_value}"


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """Load settings once per process."""

    return Settings()
