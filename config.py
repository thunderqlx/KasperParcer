from __future__ import annotations

import base64
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_bot_token: str | None = None
    telegram_user_session: str = "telegram_market_user"
    telegram_session_string: str | None = None
    telegram_session_b64: str | None = None

    request_delay_min: float = 0.25
    request_delay_max: float = 0.8
    max_checks_per_scan: int = 50
    checks_per_requested_user: int = 10
    min_checks_per_scan: int = 50

    output_dir: Path = Path("output")

    @field_validator("telegram_api_id", mode="before")
    @classmethod
    def parse_optional_int(cls, value: object) -> int | None:
        if value in (None, ""):
            return None
        return int(value)


settings = Settings()


def ensure_session_file() -> None:
    session_path = Path(f"{settings.telegram_user_session}.session")
    if session_path.exists():
        return
    if not settings.telegram_session_b64:
        return
    session_path.write_bytes(base64.b64decode(settings.telegram_session_b64))
