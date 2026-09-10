"""Shared fixtures.

The one job of this file: **the unit suite must run anywhere.** `pyproject.toml`
sets ``testpaths = ["tests"]`` and declares a ``db`` marker, but the tree shipped
with no `tests/` directory at all, so `pytest` collected nothing and CI's
`pytest tests/unit` failed on a missing path (audit #18). Now that the directory
exists, the next trap is the opposite one: a suite that only passes when a
Postgres container happens to be running on localhost:5432.

So there are exactly two classes of test here.

* **unit** -- pure Python, no I/O, no database. Runs on a laptop, on a fresh
  clone, inside the CI job that has no service container. Never marked.
* **integration** -- marked ``db``. Needs the pinned ParadeDB container. Skips
  cleanly, with a reason that names the URL it tried, when that container is not
  reachable.

The skip is decided ONCE per session by a real connection attempt with a short
timeout, and cached. A per-test connection attempt against a dead host costs the
TCP timeout every time, which turns "the database is down" into a two-minute
test run that looks like a hang.

`pace-db` is deliberately NOT started by this file. A test suite that starts
containers hides the dependency and then fails differently on the machine that
already had one running with different data.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Importable from a plain checkout, not only from an editable install. `uv sync`
# installs the project, so this is belt-and-braces -- but `pytest` invoked
# directly in a venv that predates the last `uv sync` is a real and confusing
# failure mode, and one line here is cheaper than the bug report.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Same default as `provenance.config.Settings.database_url`, with the same
#: PACE_ override, so a test run and an application run can never disagree about
#: which database is meant.
DEFAULT_DATABASE_URL = "postgresql://postgres:pace@localhost:5432/pace"

_CONNECT_TIMEOUT_SECONDS = 3

_probe_result: tuple[bool, str] | None = None


def database_url() -> str:
    return os.environ.get("PACE_DATABASE_URL", DEFAULT_DATABASE_URL)


def _probe_database() -> tuple[bool, str]:
    """(reachable, reason). Runs at most once per session."""
    global _probe_result
    if _probe_result is not None:
        return _probe_result

    url = database_url()
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - psycopg is a base dependency
        _probe_result = (False, f"psycopg is not installed ({exc})")
        return _probe_result

    try:
        with psycopg.connect(url, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # noqa: BLE001 -- any failure means "not usable"
        _probe_result = (False, f"{url} is not reachable: {type(exc).__name__}: {exc}")
    else:
        _probe_result = (True, "")
    return _probe_result


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip every ``db``-marked test when the database is not reachable.

    Done as a hook rather than only inside the fixture so that a db-marked test
    which builds its own connection (or asserts on `alembic`) still skips instead
    of erroring. `--strict-markers` is on, so a typo in the marker name is a
    collection error rather than a silently-never-skipped test.
    """
    if item.get_closest_marker("db") is None:
        return
    ok, reason = _probe_database()
    if not ok:
        pytest.skip(f"needs the pace-db container: {reason}")


@pytest.fixture(scope="session")
def db_connection() -> Iterator[Any]:
    """A plain autocommit psycopg connection to `pace-db`, or a clean skip.

    Deliberately NOT `provenance.graph.db.connection()`: that opens the
    process-wide pool, and a test that fails half way through would leave a
    pooled connection checked out for the rest of the session. A direct
    connection is torn down by this fixture's own `with` block.

    Rows come back as TUPLES, which is `graph.db.connection`'s default too, so a
    query copied between a test and the application behaves identically.
    """
    import psycopg

    ok, reason = _probe_database()
    if not ok:
        pytest.skip(f"needs the pace-db container: {reason}")

    with psycopg.connect(database_url(), connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
        conn.autocommit = True
        yield conn


@pytest.fixture(scope="session")
def migrated_database(db_connection: Any) -> Any:
    """`alembic upgrade head`, applied in-process, then the live connection.

    In-process rather than a subprocess so a migration failure surfaces as a real
    traceback in the test output. `script_location` is made absolute because
    `alembic.ini` sets it relative to the repo root and pytest may be invoked
    from anywhere.

    Re-running `upgrade head` on an already-migrated database is a no-op: every
    statement in 0001-0005 is `IF NOT EXISTS` or wrapped in
    `DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL; END $$;`, which is the
    property audit #23 restored. If this fixture ever starts failing with
    "already exists", that guard has regressed.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(config, "head")
    return db_connection
