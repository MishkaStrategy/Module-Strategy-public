"""Composed SQLite schema DDL for the immutable results registry."""

from .storage_schema_tables import _SCHEMA_TABLES_SQL
from .storage_schema_triggers import _SCHEMA_TRIGGERS_SQL

_SCHEMA_SQL = _SCHEMA_TABLES_SQL + _SCHEMA_TRIGGERS_SQL
