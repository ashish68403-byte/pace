"""Resolve an ``owner/name`` corpus identity to the bigint ``repo_id``.

The golden set, the config and every log line speak in ``apache/airflow``.
The schema speaks in ``repo_id bigint``. Passing the string straight into a
query against ``chunks.repo_id`` raises `invalid input syntax for type bigint`,
which is a clear error -- but only once the query runs, which for the eval path
means after a fixture load and a scoring loop have already happened.
"""

from __future__ import annotations

import psycopg

from provenance.graph.tables import REPOSITORIES


class UnknownRepository(RuntimeError):
    """The corpus has not been ingested into this database."""


def resolve_repo_id(conn: psycopg.Connection, owner: str, name: str) -> int:
    """Return the bigint repo_id for ``owner/name``.

    Raises rather than returning None: every caller needs the id to build a
    query, and a None would become `WHERE repo_id IS NULL`, which quietly
    matches nothing and looks like an empty corpus.
    """
    row = conn.execute(
        f"SELECT repo_id FROM {REPOSITORIES} WHERE owner = %s AND name = %s",
        (owner, name),
    ).fetchone()
    if row is None:
        raise UnknownRepository(
            f"{owner}/{name} is not in repositories. Run `pace ingest` first; "
            "an empty result here is otherwise indistinguishable from an "
            "un-ingested corpus."
        )
    return int(row[0] if isinstance(row, tuple) else row["repo_id"])


def split_repo_slug(slug: str) -> tuple[str, str]:
    """`apache/airflow` -> `("apache", "airflow")`."""
    owner, _, name = slug.partition("/")
    if not owner or not name:
        raise ValueError(f"expected 'owner/name', got {slug!r}")
    return owner, name
