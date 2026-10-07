from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FORWARDED_HEADERS = frozenset(
    {
        "x-vqd-hash-1",
        "x-vqd-4",
        "x-fe-signals",
        "x-fe-version",
        "x-ddg-journey-id",
        "user-agent",
        "accept-language",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    duckai_headers_json: dict[str, str] = Field(default_factory=dict)
    proxy_api_key: str | None = None
    duckai_auto_token: bool = True
    duckai_browser_headless: bool = True
    duckai_browser_auto_install: bool = True
    browser_install_timeout: float = Field(default=300, gt=0)
    duckai_browser_executable: str | None = None
    duckai_browser_channel: str | None = None
    duckai_browser_cdp_url: str | None = None
    token_generation_timeout: float = Field(default=30, gt=0)
    upstream_connect_timeout: float = Field(default=10, gt=0)
    upstream_read_timeout: float = Field(default=120, gt=0)
    max_collected_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_image_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=20_000_000, gt=0)

    @field_validator("duckai_headers_json")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {key.lower(): val for key, val in value.items()}
        if normalized.keys() - FORWARDED_HEADERS:
            raise ValueError("DUCKAI_HEADERS_JSON contains unsupported header names")
        if any("\r" in val or "\n" in val for val in normalized.values()):
            raise ValueError("Header values must not contain newlines")
        return normalized
