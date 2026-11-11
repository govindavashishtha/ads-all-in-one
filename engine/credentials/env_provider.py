"""Credentials from environment variables (standalone mode, CREDENTIALS_PROVIDER=env)."""

from __future__ import annotations

from pydantic import SecretStr

from engine.config import Settings
from engine.credentials.base import (
    CredentialsProvider,
    GoogleCredentials,
    MetaCredentials,
    PlatformCredentials,
)
from engine.enums import Access, Platform
from engine.errors import CredentialsUnavailableError, MissingInputError


def _is_placeholder(value: str | None) -> bool:
    """Blank, or an `.env.example` placeholder such as act_XXXXXXXXXX."""
    return value is None or value.strip() == "" or "XXXX" in value.upper()


def _need_str(value: str | None, name: str, input_id: str) -> str:
    if value is None or _is_placeholder(value):
        raise MissingInputError(input_id, f"{name} is not set")
    return value


def _need_secret(value: SecretStr | None, name: str, input_id: str) -> SecretStr:
    if value is None or value.get_secret_value().strip() == "":
        raise MissingInputError(input_id, f"{name} is not set")
    return value


class EnvCredentialsProvider(CredentialsProvider):
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def get(self, client_id: str, platform: Platform, access: Access) -> PlatformCredentials:
        if client_id != self._s.client_id:
            raise CredentialsUnavailableError(
                f"env provider only serves CLIENT_ID={self._s.client_id!r}, not {client_id!r}"
            )
        if platform is Platform.META:
            return self._meta(access)
        return self._google(access)

    def _meta(self, access: Access) -> MetaCredentials:
        s = self._s
        raw = s.meta_write_token if access is Access.WRITE else s.meta_read_token
        if access is Access.WRITE and (raw is None or not raw.get_secret_value().strip()):
            raise CredentialsUnavailableError(
                "META_WRITE_TOKEN is empty (installed only after the Phase 3 gate, §0 rule 5)"
            )
        return MetaCredentials(
            access=access,
            app_id=_need_str(s.meta_app_id, "META_APP_ID", "I-9"),
            app_secret=_need_secret(s.meta_app_secret, "META_APP_SECRET", "I-9"),
            token=_need_secret(raw, f"META_{access.value.upper()}_TOKEN", "I-9"),
            ad_account_id=_need_str(s.meta_ad_account_id, "META_AD_ACCOUNT_ID", "I-9"),
            api_version=_need_str(s.meta_api_version, "META_API_VERSION", "VERIFY-P0"),
        )

    def _google(self, access: Access) -> GoogleCredentials:
        s = self._s
        raw = (
            s.google_write_refresh_token if access is Access.WRITE else s.google_read_refresh_token
        )
        if access is Access.WRITE and (raw is None or not raw.get_secret_value().strip()):
            raise CredentialsUnavailableError(
                "GOOGLE_WRITE_REFRESH_TOKEN is empty (installed only after the Phase 5 gate)"
            )
        login = s.google_ads_login_customer_id
        return GoogleCredentials(
            access=access,
            customer_id=_need_str(s.google_ads_customer_id, "GOOGLE_ADS_CUSTOMER_ID", "I-10"),
            login_customer_id=None if _is_placeholder(login) else login,
            cloud_project_id=_need_str(
                s.google_cloud_project_id, "GOOGLE_CLOUD_PROJECT_ID", "I-10"
            ),
            oauth_client_id=_need_str(s.google_oauth_client_id, "GOOGLE_OAUTH_CLIENT_ID", "I-10"),
            oauth_client_secret=_need_secret(
                s.google_oauth_client_secret, "GOOGLE_OAUTH_CLIENT_SECRET", "I-10"
            ),
            refresh_token=_need_secret(raw, f"GOOGLE_{access.value.upper()}_REFRESH_TOKEN", "I-10"),
        )
