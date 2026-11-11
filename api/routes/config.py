"""/rules and /limits (§7.1, §17): active version + history; PUT creates a new version."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import func, select, update

from api.auth.adapter import Admin, Viewer
from api.deps import ClockDep, DbDep, SettingsDep, client_ip, load_client
from api.schemas import ConfigResponse, ConfigUpdateRequest, ConfigVersionOut, ConfigVersionSummary
from api.services.config import active_version
from api.services.queries import audit
from engine.config import Settings
from engine.db.models import Client, ConfigVersion
from engine.enums import ConfigKind
from engine.errors import ConfigError, MissingInputError
from engine.guardrails.limits_schema import parse_limits
from engine.rules.schema import parse_rules

router = APIRouter(tags=["config"])


def _read(db: Any, client_id: str, kind: ConfigKind) -> ConfigResponse:
    active = active_version(db, client_id, kind)
    history = db.scalars(
        select(ConfigVersion)
        .where(ConfigVersion.client_id == client_id, ConfigVersion.kind == kind.value)
        .order_by(ConfigVersion.version.desc())
    ).all()
    return ConfigResponse(
        kind=kind.value,
        active=ConfigVersionOut.model_validate(active) if active else None,
        history=[ConfigVersionSummary.model_validate(h) for h in history],
    )


def _validate(kind: ConfigKind, body: dict[str, Any], settings: Settings, client: Client) -> None:
    """Schema, registry names (rules), HARD_* ceilings (limits), currency (rules)."""
    try:
        if kind is ConfigKind.RULES:
            rules = parse_rules(body)
            if rules.targets.currency != client.currency:
                raise HTTPException(
                    422,
                    f"targets.currency {rules.targets.currency} differs from the client "
                    f"currency {client.currency}",
                )
        else:
            limits = parse_limits(body)
            hard_daily, hard_monthly = settings.require_hard_caps()
            limits.check_hard_ceilings(hard_daily, hard_monthly)
    except ValidationError as exc:
        raise HTTPException(
            422, [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        ) from exc
    except ConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    except MissingInputError as exc:
        raise HTTPException(503, str(exc)) from exc


def _put(
    kind: ConfigKind,
    *,
    body: ConfigUpdateRequest,
    request: Request,
    user: Any,
    db: Any,
    settings: Settings,
    now: Any,
) -> ConfigResponse:
    client = load_client(db, settings)
    # Serialise concurrent PUTs for this client+kind.
    db.execute(select(Client).where(Client.client_id == client.client_id).with_for_update())
    current = db.scalar(
        select(func.max(ConfigVersion.version)).where(
            ConfigVersion.client_id == client.client_id, ConfigVersion.kind == kind.value
        )
    )
    new_version = int(current or 0) + 1
    new_body = {**body.body, "version": new_version}
    _validate(kind, new_body, settings, client)
    prev = active_version(db, client.client_id, kind)
    db.execute(
        update(ConfigVersion)
        .where(
            ConfigVersion.client_id == client.client_id,
            ConfigVersion.kind == kind.value,
            ConfigVersion.active.is_(True),
        )
        .values(active=False)
    )
    db.flush()
    db.add(
        ConfigVersion(
            client_id=client.client_id,
            kind=kind.value,
            version=new_version,
            body=new_body,
            active=True,
            changed_by=user.username,
            reason=body.reason,
        )
    )
    audit(
        db,
        client_id=client.client_id,
        actor=user.username,
        action=f"config.{kind.value}.update",
        target=f"config:{kind.value}:v{new_version}",
        payload={"previous_version": prev.version if prev else None, "reason": body.reason},
        ip=client_ip(request),
        at=now,
    )
    db.commit()
    return _read(db, client.client_id, kind)


@router.get("/rules", response_model=ConfigResponse)
def get_rules(*, user: Viewer, db: DbDep, settings: SettingsDep) -> ConfigResponse:
    return _read(db, settings.client_id, ConfigKind.RULES)


@router.get("/limits", response_model=ConfigResponse)
def get_limits(*, user: Viewer, db: DbDep, settings: SettingsDep) -> ConfigResponse:
    return _read(db, settings.client_id, ConfigKind.LIMITS)


@router.put("/rules", response_model=ConfigResponse)
def put_rules(
    *,
    body: ConfigUpdateRequest,
    request: Request,
    user: Admin,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> ConfigResponse:
    """New rules version (validated against schema + variable registry), audited."""
    return _put(
        ConfigKind.RULES,
        body=body,
        request=request,
        user=user,
        db=db,
        settings=settings,
        now=clock(),
    )


@router.put("/limits", response_model=ConfigResponse)
def put_limits(
    *,
    body: ConfigUpdateRequest,
    request: Request,
    user: Admin,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> ConfigResponse:
    """New limits version; rejected if any cap exceeds a HARD_* ceiling (§7.1), audited."""
    return _put(
        ConfigKind.LIMITS,
        body=body,
        request=request,
        user=user,
        db=db,
        settings=settings,
        now=clock(),
    )
