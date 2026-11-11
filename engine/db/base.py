"""Declarative base + column conventions (plan §8.1)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    MetaData,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from engine.enums import EntityType, Platform

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Native Postgres enum that stores the enum *values* (lower-case strings)."""
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


PLATFORM_ENUM = pg_enum(Platform, "platform")
ENTITY_TYPE_ENUM = pg_enum(EntityType, "entity_type")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012
        str: Text(),
        int: BigInteger(),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB(),
    }


class ClientScoped:
    """Every table has client_id TEXT NOT NULL and created_at TIMESTAMPTZ DEFAULT now() (§8.1)."""

    client_id: Mapped[str] = mapped_column(
        Text, ForeignKey("clients.client_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def identity_pk() -> Mapped[int]:
    """id BIGINT GENERATED ALWAYS AS IDENTITY (§8.1)."""
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)
