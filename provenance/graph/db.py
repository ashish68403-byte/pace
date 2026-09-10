"""psycopg 3 connection pool, vector type registration, and healthcheck.

What this module is for: one process-wide pool, opened lazily, with pgvector
adapters registered on every connection the pool hands out.

What failure it prevents:
  * Connection-per-query. On 2 physical cores a fresh TCP + TLS + auth
    handshake per request dominates the latency of an ingest loop.
  * register_vector() called once, on one connection, and then forgotten.
    psycopg registers adapters per connection; a pooled connection created
    later would return vectors as strings, and numpy would happily build an
    array of shape () from one. Registration is in the pool's `configure`
    hook so it applies to every connection, including replacements.
  * A silent version drift between what the migrations assume and what the
    server actually has. healthcheck() reports the server version and the
    installed extension versions, and names any required extension that is
    missing rather than letting the first query fail at 3am.

Plain SQL only -- no SQLAlchemy ORM. The schema uses domains, expression
indexes, exclusion constraints and pdb/halfvec casts that an ORM would have to
be fought into emitting.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg_pool import ConnectionPool

from provenance.config import settings

# Extensions 0001_core creates. healthcheck() reports any that are absent;
# every one of them is load bearing (bm25 index, HNSW, trigram link recovery,
# the blame_ranges exclusion constraint).
REQUIRED_EXTENSIONS: tuple[str, ...] = ("vector", "pg_search", "pg_trgm", "btree_gist")

# 2 physical cores / 4 threads and ~3.7 GB of RAM visible to WSL. A larger pool
# does not buy throughput here -- it buys context switching and a bigger
# work_mem multiplier on the server side.
_MIN_SIZE = 1
_MAX_SIZE = 4

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _configure(conn: psycopg.Connection[Any]) -> None:
    """Per-connection setup run by the pool for every connection it creates."""
    try:
        from pgvector.psycopg import register_vector

        register_vector(conn)
    except psycopg.ProgrammingError:
        # The `vector` type does not exist yet -- this is a fresh database
        # before `alembic upgrade head`. Let the connection through so the
        # migration can run; healthcheck() is what reports the gap.
        conn.rollback()


def get_pool() -> ConnectionPool:
    """Return the process-wide pool, creating it on first use."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                pool = ConnectionPool(
                    conninfo=settings.database_url,
                    min_size=_MIN_SIZE,
                    max_size=_MAX_SIZE,
                    max_idle=300.0,
                    timeout=10.0,
                    configure=_configure,
                    kwargs={"application_name": settings.service_name},
                    open=False,
                    name="pace",
                )
                # Non-blocking open: importing this module must not require the
                # database to be up. A caller that actually needs a connection
                # gets a PoolTimeout, which is a loud, specific failure.
                pool.open()
                _pool = pool
    return _pool


def close_pool() -> None:
    """Close the pool. For test teardown and CLI shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def connection(
    *,
    autocommit: bool = False,
    row_factory: Any = tuple_row,
) -> Iterator[psycopg.Connection[Any]]:
    """Check out a pooled connection.

    Rows are TUPLES by default. Nine call sites across retrieval, eval and
    ingest index rows positionally (`row[0]`, `for name, ver in ...`); a
    dict_row default made every one of them raise KeyError: 0 -- or worse,
    silently unpack dict KEYS instead of values, which is how /healthz came to
    report extensions literally named "extname". Pass row_factory=dict_row at
    the call site when you want mapping access.

    Commits on clean exit, rolls back on exception (the pool's own semantics).
    `autocommit=True` is for statements that cannot run inside a transaction
    block -- VACUUM, CREATE INDEX CONCURRENTLY. The previous setting is
    restored before the connection goes back to the pool: leaving autocommit on
    would silently change transaction semantics for whoever checks it out next.
    """
    with get_pool().connection() as conn:
        previous_autocommit = conn.autocommit
        previous_row_factory = conn.row_factory
        if autocommit != previous_autocommit:
            conn.autocommit = autocommit
        conn.row_factory = row_factory
        try:
            yield conn
        finally:
            conn.row_factory = previous_row_factory
            if autocommit != previous_autocommit:
                conn.autocommit = previous_autocommit


def healthcheck() -> dict[str, Any]:
    """Report server version, installed extensions and any missing requirement.

    Deliberately does not raise on a missing extension: the caller (the /health
    endpoint, `pace doctor`) decides what to do with a degraded database. It
    does raise if the server is unreachable -- that is not a health report, it
    is an outage.
    """
    # dict rows HERE ONLY: healthcheck reads by name (row["present"],
    # r["extname"]). Every other consumer in the tree indexes positionally,
    # which is why connection() defaults to tuple_row.
    with connection(autocommit=True, row_factory=dict_row) as conn:
        server = conn.execute(
            """
            SELECT current_setting('server_version')     AS server_version,
                   current_setting('server_version_num')::int AS server_version_num,
                   current_database()                    AS database,
                   current_user                          AS username
            """
        ).fetchone()
        rows = conn.execute(
            "SELECT extname, extversion FROM pg_extension ORDER BY extname"
        ).fetchall()
        # to_regclass() first: referencing alembic_version directly would raise
        # UndefinedTable on a database that has never been migrated, which is
        # exactly the state the healthcheck exists to describe.
        has_alembic = conn.execute(
            "SELECT to_regclass('public.alembic_version') IS NOT NULL AS present"
        ).fetchone()
        migration = (
            conn.execute("SELECT version_num FROM alembic_version").fetchall()
            if has_alembic and has_alembic["present"]
            else []
        )

    assert server is not None  # current_database() always returns a row
    extensions: dict[str, str] = {r["extname"]: r["extversion"] for r in rows}
    missing = [name for name in REQUIRED_EXTENSIONS if name not in extensions]

    return {
        "ok": not missing,
        "server_version": server["server_version"],
        "server_version_num": server["server_version_num"],
        "database": server["database"],
        "username": server["username"],
        "extensions": extensions,
        "missing_extensions": missing,
        "alembic_version": migration[0]["version_num"] if migration else None,
        "pool": {"min_size": _MIN_SIZE, "max_size": _MAX_SIZE},
    }
