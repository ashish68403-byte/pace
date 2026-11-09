"""0010_line_log: the commits `git log -L` lists for a function at a snapshot (slice C2).

Revision ID: 0010_line_log
Revises: 0009_github_bots

Arm L of ADR-0005's comparison is `git log --first-parent -L` on each question's function
at the pinned commit (ADR-0005's addendum of 2026-11-09;
docs/tasks/symbols-blame/001_blame_arms.md). One row per (function, listed commit), with
the command and git version that produced it, so slice C4 reads the arm instead of running
git again. Every listed commit is stored, mechanical ones included: the arm drops those when
it is read (ADR-0005 §2). `edge_symbol_commit` cannot hold these rows: it is keyed on
lineage ids, which only slice C3 creates.

Additive. Downgrade drops the table.
"""

from __future__ import annotations

from alembic import op

revision: str = "0010_line_log"
down_revision: str | None = "0009_github_bots"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS line_log_commits (
            repo_id        bigint      NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            file_id        bigint      NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
            snapshot_sha   git_sha     NOT NULL,
            qualified_name text        NOT NULL,
            commit_sha     git_sha     NOT NULL,
            algo           text        NOT NULL,
            computed_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (repo_id, file_id, snapshot_sha, qualified_name, commit_sha),
            CONSTRAINT line_log_commits_snapshot_fk
                FOREIGN KEY (repo_id, snapshot_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT line_log_commits_commit_fk
                FOREIGN KEY (repo_id, commit_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS line_log_commits")
