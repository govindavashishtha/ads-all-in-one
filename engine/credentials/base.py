"""CredentialsProvider interface (plan §3, §20, §24).

``get_credentials(client_id, platform, access)`` is the ONLY way any code obtains platform
secrets. ``access=write`` is requested only by the executor and the pacing emergency path.
Secrets are ``SecretStr`` so they never appear in reprs or logs.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from pydantic import SecretStr

from engine.enums import Access, Platform


@dataclass(frozen=True, slots=True)
class MetaCredentials:
    access: Access
    app_id: str
    app_secret: SecretStr
    token: SecretStr  # system-user token (§4.3): read = ads_read; write = + ads_management
    ad_account_id: str  # "act_<digits>"
    api_version: str


@dataclass(frozen=True, slots=True)
class GoogleCredentials:
    access: Access
    customer_id: str
    login_customer_id: str | None  # manager account, only if access is via a manager (I-10)
    cloud_project_id: str
    oauth_client_id: str
    oauth_client_secret: SecretStr
    refresh_token: SecretStr


PlatformCredentials = MetaCredentials | GoogleCredentials


class CredentialsProvider(abc.ABC):
    @abc.abstractmethod
    def get(self, client_id: str, platform: Platform, access: Access) -> PlatformCredentials:
        """Return credentials or raise CredentialsUnavailableError / MissingInputError."""
