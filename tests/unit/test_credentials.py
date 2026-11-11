from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from engine.config import Settings
from engine.credentials import GoogleCredentials, MetaCredentials, get_credentials
from engine.enums import Access, Platform
from engine.errors import CredentialsUnavailableError, MissingInputError

FAKE_META: dict[str, Any] = {  # obviously fake test values
    "meta_app_id": "1",
    "meta_app_secret": "fake-secret",
    "meta_read_token": "fake-read",
    "meta_ad_account_id": "act_1",
    "meta_api_version": "vTEST",
}
FAKE_GOOGLE: dict[str, Any] = {
    "google_ads_customer_id": "1",
    "google_cloud_project_id": "p",
    "google_oauth_client_id": "c",
    "google_oauth_client_secret": "fake",
    "google_read_refresh_token": "fake-refresh",
}


def test_meta_read(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(**FAKE_META)
    creds = get_credentials("client_test", "meta", "read", settings=s)
    assert isinstance(creds, MetaCredentials)
    assert creds.access is Access.READ
    assert "fake-read" not in repr(creds)


def test_write_token_empty_until_phase_gate(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(**FAKE_META, **FAKE_GOOGLE)
    with pytest.raises(CredentialsUnavailableError):
        get_credentials("client_test", Platform.META, Access.WRITE, settings=s)
    with pytest.raises(CredentialsUnavailableError):
        get_credentials("client_test", Platform.GOOGLE, Access.WRITE, settings=s)


def test_missing_and_placeholder_values_fail_fast(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(**{**FAKE_META, "meta_ad_account_id": "act_XXXXXXXXXX"})
    with pytest.raises(MissingInputError) as exc:
        get_credentials("client_test", "meta", "read", settings=s)
    assert exc.value.input_id == "I-9"
    with pytest.raises(MissingInputError):
        get_credentials("client_test", "google", "read", settings=make_settings())


def test_google_read_and_manager_placeholder(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(**FAKE_GOOGLE)
    creds = get_credentials("client_test", "google", "read", settings=s)
    assert isinstance(creds, GoogleCredentials)
    assert creds.login_customer_id is None


def test_client_mismatch_and_remote_stub(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(CredentialsUnavailableError):
        get_credentials("other", "meta", "read", settings=make_settings(**FAKE_META))
    remote = make_settings(credentials_provider="remote")
    with pytest.raises(NotImplementedError):
        get_credentials("client_test", "meta", "read", settings=remote)
