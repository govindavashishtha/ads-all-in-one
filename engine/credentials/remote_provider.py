"""Credentials from the main backend (Phase 6, CREDENTIALS_PROVIDER=remote). NOT IMPLEMENTED.

Contract (plan §24 seam 2), to be confirmed with the main-system team (INPUT I-11):
- call ``MAIN_BACKEND_URL`` with ``MAIN_BACKEND_SERVICE_TOKEN`` for (client_id, platform, access);
- map the response to ``MetaCredentials`` / ``GoogleCredentials``;
- cache in memory for a short TTL, never write to disk; token refresh stays the backend's job.
"""

from __future__ import annotations

from engine.config import Settings
from engine.credentials.base import CredentialsProvider, PlatformCredentials
from engine.enums import Access, Platform


class RemoteCredentialsProvider(CredentialsProvider):
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def get(self, client_id: str, platform: Platform, access: Access) -> PlatformCredentials:
        raise NotImplementedError(
            "RemoteCredentialsProvider is a Phase 6 deliverable (BUILD_PLAN §24); "
            "set CREDENTIALS_PROVIDER=env"
        )
