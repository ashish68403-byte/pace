"""0011_lineage_stages: the stages the lineage engine records (slice C3a).

Revision ID: 0011_lineage_stages
Revises: 0010_line_log

`symbol_versions.match_stage` admitted only hash rungs and manual or rename-detected matches,
which assumed matching by hash alone. The engine (ADR-0005 §7 and its Errata of 2026-11-09;
docs/tasks/function-identity/002_lineage_engine.md) records how each version kept its identity:
at birth, by its name, by a name-blind h1 or h3, or by a move between files.

Widens the check. Downgrade narrows it again, which fails while a row uses a new stage.
"""

from __future__ import annotations

from alembic import op

revision: str = "0011_lineage_stages"
down_revision: str | None = "0010_line_log"
branch_labels: str | None = None
depends_on: str | None = None

_BEFORE = ("h0", "h1", "h2", "h3", "rename_detect", "manual")
_AFTER = (*_BEFORE, "birth", "name", "move")


def _set_stages(stages: tuple[str, ...]) -> None:
    listed = ", ".join(f"'{stage}'" for stage in stages)
    op.execute("ALTER TABLE symbol_versions DROP CONSTRAINT IF EXISTS symbol_versions_stage_known")
    op.execute(
        "ALTER TABLE symbol_versions ADD CONSTRAINT symbol_versions_stage_known "
        f"CHECK (match_stage IN ({listed}))"
    )


def upgrade() -> None:
    _set_stages(_AFTER)


def downgrade() -> None:
    _set_stages(_BEFORE)
