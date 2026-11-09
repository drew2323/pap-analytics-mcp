"""Persistence of the canonical model: DuckDB catalog + Parquet signals."""

from pap_analytics.store.duckdb_store import Store
from pap_analytics.store.types import (
    IngestLogEntry,
    SchemaMismatchError,
    SessionInfo,
    StoreLockedError,
    StoreReadOnlyError,
)

__all__ = [
    "IngestLogEntry",
    "SchemaMismatchError",
    "SessionInfo",
    "Store",
    "StoreLockedError",
    "StoreReadOnlyError",
]
