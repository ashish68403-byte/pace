"""0006_history_columns: what the history write path needs from the schema.

Revision ID: 0006_history_columns
Revises: 0005_chunk_evidence

Two changes, both found while specifying the write path
(docs/tasks/ingest/001_commit_graph.md and 002_file_history.md):

* commits.insertions / deletions and file_revisions.insertions / deletions were
  NOT NULL DEFAULT 0, and nothing computes them. A stored 0 reads as "this commit
  changed no lines" -- a plausible fiction. They become nullable with no default:
  NULL means "not measured".
* file_revisions had no column for the path a file had at a given commit. old_path
  holds only a rename's source (file_revisions_rename_has_old_path depends on that
  meaning), so a 2019 change to airflow/models/dag.py would be stored with no record
  of that name. path_at_commit is that record.

path_at_commit is added NOT NULL with no default, which only succeeds on an empty
table. That is deliberate: there is no correct value to backfill from inside the
database, and a migration that invented one would be worse than one that refuses.
"""

from __future__ import annotations

from alembic import op

revision: str = "0006_history_columns"
down_revision: str | None = "0005_chunk_evidence"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE commits
            ALTER COLUMN insertions DROP NOT NULL,
            ALTER COLUMN insertions DROP DEFAULT,
            ALTER COLUMN deletions  DROP NOT NULL,
            ALTER COLUMN deletions  DROP DEFAULT;

        ALTER TABLE file_revisions
            ALTER COLUMN insertions DROP NOT NULL,
            ALTER COLUMN insertions DROP DEFAULT,
            ALTER COLUMN deletions  DROP NOT NULL,
            ALTER COLUMN deletions  DROP DEFAULT,
            ADD COLUMN IF NOT EXISTS path_at_commit text NOT NULL;
        """
    )


def downgrade() -> None:
    # Restoring NOT NULL DEFAULT 0 means writing 0 into rows that were never
    # measured. Acceptable only here: a downgrade restores the old schema's
    # semantics, fiction included, and says so.
    op.execute(
        """
        ALTER TABLE file_revisions DROP COLUMN IF EXISTS path_at_commit;

        UPDATE file_revisions SET insertions = 0 WHERE insertions IS NULL;
        UPDATE file_revisions SET deletions  = 0 WHERE deletions  IS NULL;
        ALTER TABLE file_revisions
            ALTER COLUMN insertions SET DEFAULT 0,
            ALTER COLUMN insertions SET NOT NULL,
            ALTER COLUMN deletions  SET DEFAULT 0,
            ALTER COLUMN deletions  SET NOT NULL;

        UPDATE commits SET insertions = 0 WHERE insertions IS NULL;
        UPDATE commits SET deletions  = 0 WHERE deletions  IS NULL;
        ALTER TABLE commits
            ALTER COLUMN insertions SET DEFAULT 0,
            ALTER COLUMN insertions SET NOT NULL,
            ALTER COLUMN deletions  SET DEFAULT 0,
            ALTER COLUMN deletions  SET NOT NULL;
        """
    )
