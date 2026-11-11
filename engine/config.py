"""Settings from the environment (plan §7). Only this module reads env vars.

Rules (§7.1):
- env seeds mode/rules/limits on first start only; the DB is the runtime source of truth;
- ``HARD_*`` ceilings and ``KILL_SWITCH`` are env-only overrides the API can never relax;
- secrets are ``SecretStr`` and must only be unwrapped by ``engine.credentials`` (§20).
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from engine.enums import Mode
from engine.errors import MissingInputError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- App ---
    app_env: Literal["development", "staging", "production"] = "development"
    client_id: str = "client_001"
    client_name: str = "Client Name"
    timezone: str = "Asia/Kolkata"
    initial_mode: Mode = Mode.READ_ONLY
    kill_switch: bool = False

    # --- Hard ceilings (major currency units, total across platforms) ---
    hard_daily_cap: Decimal | None = None  # INPUT I-2
    hard_monthly_cap: Decimal | None = None  # INPUT I-2
    hard_max_mode: Mode = Mode.SUGGEST

    # --- Auth ---
    auth_mode: Literal["env", "main_system"] = "env"
    admin_user: str = "admin"
    admin_password_hash: SecretStr | None = None
    secret_key: SecretStr | None = None
    session_ttl_minutes: int = Field(default=720, gt=0)
    main_system_jwks_url: str | None = None

    # --- Credentials source ---
    credentials_provider: Literal["env", "remote"] = "env"
    main_backend_url: str | None = None
    main_backend_service_token: SecretStr | None = None

    # --- Meta ---
    meta_app_id: str | None = None
    meta_app_secret: SecretStr | None = None
    meta_read_token: SecretStr | None = None
    meta_write_token: SecretStr | None = None
    meta_ad_account_id: str | None = None
    meta_api_version: str | None = None

    # --- Google Ads ---
    google_ads_customer_id: str | None = None
    google_ads_login_customer_id: str | None = None
    google_cloud_project_id: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    google_read_refresh_token: SecretStr | None = None
    google_write_refresh_token: SecretStr | None = None

    # --- GA4 ---
    ga4_property_id: str | None = None
    ga4_service_account_json_path: str | None = None

    # --- LLM (INPUT I-8; model names are never defaulted) ---
    llm_provider: str | None = None
    llm_api_key: SecretStr | None = None
    llm_small_model: str | None = None
    llm_large_model: str | None = None
    llm_small_in_price_per_mtok: Decimal | None = None
    llm_small_out_price_per_mtok: Decimal | None = None
    llm_large_in_price_per_mtok: Decimal | None = None
    llm_large_out_price_per_mtok: Decimal | None = None
    llm_daily_token_cap: int = 100_000
    llm_daily_cost_cap_usd: Decimal = Decimal("1.00")
    llm_monthly_cost_cap_usd: Decimal = Decimal("20.00")

    # --- Database ---
    database_url: str = "postgresql+psycopg://ads:ads@postgres:5432/ads_autopilot"

    # --- Notifications ---
    slack_webhook_url: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    alert_email_to: str | None = None
    whatsapp_provider: str | None = None
    whatsapp_api_key: SecretStr | None = None
    whatsapp_to: str | None = None

    # --- Observability ---
    heartbeat_url: str | None = None
    metrics_token: SecretStr | None = None
    sentry_dsn: SecretStr | None = None

    # --- Frontend (build-time; read here only for reference/validation) ---
    next_public_base_path: str = "/ads-autopilot"
    next_public_api_url: str = "/ads-autopilot/api/v1"

    @model_validator(mode="before")
    @classmethod
    def _blank_is_none(cls, data: Any) -> Any:
        """`.env.example` leaves unset values blank; blank means "not provided" (use default)."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and v.strip() == "")}
        return data

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"TIMEZONE must be an IANA zone, got {v!r}") from exc
        return v

    @field_validator("hard_daily_cap", "hard_monthly_cap")
    @classmethod
    def _positive(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("HARD_* caps must be > 0")
        return v

    @model_validator(mode="after")
    def _initial_mode_within_ceiling(self) -> Settings:
        if self.initial_mode.exceeds(self.hard_max_mode):
            raise ValueError(
                f"INITIAL_MODE={self.initial_mode} is above HARD_MAX_MODE={self.hard_max_mode}"
            )
        return self

    # ---- derived helpers (no business logic) ----

    def require_hard_caps(self) -> tuple[Decimal, Decimal]:
        """Fail fast if the env ceilings (INPUT I-2) are not set."""
        if self.hard_daily_cap is None or self.hard_monthly_cap is None:
            raise MissingInputError(
                "I-2", "HARD_DAILY_CAP and HARD_MONTHLY_CAP must be set in the environment"
            )
        return self.hard_daily_cap, self.hard_monthly_cap

    def mode_allowed(self, requested: Mode) -> bool:
        """`/control/mode` rule (§7.1): never above HARD_MAX_MODE."""
        return not requested.exceeds(self.hard_max_mode)

    def effective_kill_switch(self, db_kill_switch: bool) -> bool:
        """Kill switch = DB flag OR env override (§7.1). The API can't clear the env one."""
        return self.kill_switch or db_kill_switch


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
