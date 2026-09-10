"""Alembic environment for PACE.

What this module is for: point Alembic at exactly the same database the
application uses, with the same driver, and nothing else.

What failure it prevents:

1.  A migration run against a *different* database than the app reads. The URL
    is taken from ``provenance.config.settings`` (env prefix ``PACE_``), never
    from ``alembic.ini``. There is one source of truth.

2.  ``ModuleNotFoundError: No module named 'psycopg2'``. SQLAlchemy maps a bare
    ``postgresql://`` URL onto the psycopg2 dialect, and psycopg2 is not a
    dependency of this project — we are on psycopg 3. We rewrite the scheme to
    ``postgresql+psycopg://`` here so the app-facing URL can stay driver-neutral.

3.  Half-applied revisions. Every revision runs inside one transaction
    (``transaction_per_migration``), so a failure at statement 12 of 20 leaves
    the schema and ``alembic_version`` consistent. Postgres has transactional
    DDL; this is the entire reason the raw-SQL approach is safe.

Note on CREATE INDEX CONCURRENTLY: it cannot run inside a transaction block, so
it does not appear in any revision here. On a real 5M-row table you build those
out-of-band and let the migration only record the fact.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from provenance.config import settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Raw SQL revisions -> no metadata, no autogenerate. See alembic.ini.
target_metadata = None


def _sqlalchemy_url() -> str:
    """Return settings.database_url pinned to the psycopg 3 dialect."""
    url = settings.database_url
    if url.startswith("postgresql+"):
        pass
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade head --sql``)."""
    context.configure(
        url=_sqlalchemy_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
        compare_type=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply revisions."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sqlalchemy_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=True,
            compare_type=False,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
