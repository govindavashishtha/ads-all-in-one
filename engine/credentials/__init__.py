"""Single entry point for platform credentials: ``get_credentials(client_id, platform, access)``."""

from __future__ import annotations

from engine.config import Settings, get_settings
from engine.credentials.base import (
    CredentialsProvider,
    GoogleCredentials,
    MetaCredentials,
    PlatformCredentials,
)
from engine.enums import Access, Platform

__all__ = [
    "CredentialsProvider",
    "GoogleCredentials",
    "MetaCredentials",
    "PlatformCredentials",
    "get_credentials",
    "provider_for",
]


def provider_for(settings: Settings) -> CredentialsProvider:
    if settings.credentials_provider == "remote":
        from engine.credentials.remote_provider import RemoteCredentialsProvider

        return RemoteCredentialsProvider(settings)
    from engine.credentials.env_provider import EnvCredentialsProvider

    return EnvCredentialsProvider(settings)


def get_credentials(
    client_id: str,
    platform: Platform | str,
    access: Access | str,
    *,
    settings: Settings | None = None,
) -> PlatformCredentials:
    s = settings or get_settings()
    return provider_for(s).get(client_id, Platform(platform), Access(access))
