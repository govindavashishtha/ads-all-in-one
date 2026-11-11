"""Channel adapters behind one interface (plan §5 Notifications, §14).

Phase 2 ships **pluggable stubs**: every adapter reports whether its env settings are present
(``configured``) but performs no network I/O unless a transport is explicitly injected.
No transport is wired by default because the approval channel is still an open input (I-5)
and channel secrets must flow through ``engine.credentials`` (AGENT_GUIDE rule 8). So with
the default registry every notification is written to the structured log only (channel
``log``) — nothing is ever sent to Slack, email or WhatsApp by accident.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from engine.config import Settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    notification_id: int
    kind: str
    subject: str
    body_text: str
    body_html: str | None


Transport = Callable[[OutboundMessage], None]


class Channel(Protocol):
    name: str

    def configured(self, settings: Settings) -> bool: ...

    def deliverable(self, settings: Settings) -> bool: ...

    def send(self, msg: OutboundMessage) -> None: ...


class _StubChannel:
    name = "stub"

    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport

    def configured(self, settings: Settings) -> bool:
        raise NotImplementedError

    def deliverable(self, settings: Settings) -> bool:
        """Only when configured AND a transport was injected (never by default)."""
        return self.transport is not None and self.configured(settings)

    def send(self, msg: OutboundMessage) -> None:
        if self.transport is None:  # pragma: no cover - guarded by deliverable()
            raise RuntimeError(f"{self.name}: no transport wired")
        self.transport(msg)


class SlackChannel(_StubChannel):
    name = "slack"

    def configured(self, settings: Settings) -> bool:
        return settings.slack_webhook_url is not None


class EmailChannel(_StubChannel):
    name = "email"

    def configured(self, settings: Settings) -> bool:
        return bool(settings.smtp_host and settings.alert_email_to)


class WhatsAppChannel(_StubChannel):
    name = "whatsapp"

    def configured(self, settings: Settings) -> bool:
        return bool(
            settings.whatsapp_provider
            and settings.whatsapp_api_key is not None
            and settings.whatsapp_to
        )


class LogChannel:
    """Always available fallback: one structured log line per notification, no I/O."""

    name = "log"

    def configured(self, settings: Settings) -> bool:
        return True

    def deliverable(self, settings: Settings) -> bool:
        return True

    def send(self, msg: OutboundMessage) -> None:
        log.info(
            "notify.logged",
            notification_id=msg.notification_id,
            kind=msg.kind,
            subject=msg.subject,
            chars=len(msg.body_text),
        )


def default_channels() -> list[Channel]:
    return [SlackChannel(), EmailChannel(), WhatsAppChannel()]


def configured_channel_names(
    settings: Settings, channels: list[Channel] | None = None
) -> list[str]:
    return [c.name for c in (channels or default_channels()) if c.configured(settings)]
