"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

PACE revisions are raw SQL executed through op.execute(). Rules:
  * every upgrade() has a real, tested downgrade() -- `alembic downgrade base
    && alembic upgrade head` must be runnable twice in a row, clean;
  * DDL is guarded (IF NOT EXISTS / IF EXISTS, or a DO block trapping
    duplicate_object) because CREATE DOMAIN and CREATE TYPE have no
    IF NOT EXISTS form;
  * no CREATE INDEX CONCURRENTLY -- it cannot run inside a transaction.
"""

from __future__ import annotations

from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


def upgrade() -> None:
    op.execute(
        """
        ${"-- SQL here"}
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ${"-- inverse SQL here"}
        """
    )
