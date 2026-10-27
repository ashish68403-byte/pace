"""0007_rationale_evidence: a document can BE an artifact, or belong to one.

Revision ID: 0007_rationale_evidence
Revises: 0006_history_columns

ADR-0003 makes the benchmark's pool rationale text -- commit messages first, PR
descriptions and review comments after the GitHub backfill -- stored as rows in
`chunks`. Their links in `chunk_evidence` need two sources the 0005 CHECK does not
allow:

* self   -- the document is that artifact's own text (a commit message for its commit);
* linked -- the document belongs to an artifact joined by an edge (a commit message
            whose subject carries (#N) is also evidence for PR N).

The downgrade restores the five-value CHECK and fails loudly while any row still uses
`self` or `linked`. That is deliberate: deleting those rows to make the constraint fit
would be data loss dressed up as a schema change.
"""

from __future__ import annotations

from alembic import op

revision: str = "0007_rationale_evidence"
down_revision: str | None = "0006_history_columns"
branch_labels: str | None = None
depends_on: str | None = None

_CODE_SOURCES = "'blame', 'ast_identity', 'diff', 'lineage_edge', 'manual'"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE chunk_evidence DROP CONSTRAINT IF EXISTS chunk_evidence_source_check;
        ALTER TABLE chunk_evidence ADD CONSTRAINT chunk_evidence_source_check
            CHECK (source IN ({_CODE_SOURCES}, 'self', 'linked'));
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE chunk_evidence DROP CONSTRAINT IF EXISTS chunk_evidence_source_check;
        ALTER TABLE chunk_evidence ADD CONSTRAINT chunk_evidence_source_check
            CHECK (source IN ({_CODE_SOURCES}));
        """
    )
