"""Graph layer: content-addressed identity and database access.

`ids` is pure stdlib and safe to import anywhere (the eval harness imports it
without a database). `db` pulls in psycopg and the pool, so it is exposed
through a module-level __getattr__ instead of a top-level import -- importing
`provenance.graph.ids` must not drag a driver, a pool and a config read into a
process that only wanted to hash a string.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from provenance.graph.ids import CHUNK_ID_SCHEME, chunk_id, normalize_content

if TYPE_CHECKING:  # pragma: no cover - typing only
    from provenance.graph.db import close_pool, connection, get_pool, healthcheck

__all__ = [
    "CHUNK_ID_SCHEME",
    "chunk_id",
    "close_pool",
    "connection",
    "get_pool",
    "healthcheck",
    "normalize_content",
]

_LAZY = {"connection", "get_pool", "close_pool", "healthcheck"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from provenance.graph import db

        return getattr(db, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
