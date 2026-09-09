"""Declarative base and metadata for the PostgreSQL snapshot schema."""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# PostgreSQL authoritative schema is 'snapshot'
SCHEMA_NAME = "snapshot"
metadata = MetaData(schema=SCHEMA_NAME)


class Base(DeclarativeBase):
    """Base class for all Snapshot Backend ORM models."""

    metadata = metadata

