from decimal import Decimal
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PostgresSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POSTGRES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    db: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    api_key: SecretStr = Field(min_length=1)
    model: str = Field(default="qwen/qwen3.8-27b", min_length=1)
    timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    max_input_chars: int = Field(default=24_000, gt=0)
    max_output_tokens: int = Field(default=2_048, ge=16)
    classification_max_output_tokens: int | None = Field(default=None, ge=16)
    summary_max_output_tokens: int | None = Field(default=None, ge=16)
    input_usd_per_million: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    cached_input_usd_per_million: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    output_usd_per_million: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )

    @field_validator("api_key")
    @classmethod
    def check_api_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if not key or not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError("GROQ_API_KEY must be a non-empty token.")
        return SecretStr(key)

    @model_validator(mode="after")
    def check_prices(self) -> Self:
        if (self.input_usd_per_million is None) != (
            self.output_usd_per_million is None
        ):
            raise ValueError("Set both input and output prices to estimate cost.")
        if (
            self.cached_input_usd_per_million is not None
            and self.input_usd_per_million is None
        ):
            raise ValueError("A cached input price requires input and output prices.")
        return self
