"""0009_github_bots: whether GitHub marks a PR's or a comment's author as a bot.

Revision ID: 0009_github_bots
Revises: 0008_hashed_emails

Slice B4 (docs/tasks/eval-harness/004_github_backfill.md) keeps bot-written text out of the
benchmark's pool (ADR-0003, second 2026-10-29 addendum): a welcome or stale-bot message would
otherwise count as gold for every PR it appears on. GitHub says which accounts are bots
(`user.type == "Bot"`); a rule on login names (`...[bot]`) can miss one. The backfill stores
GitHub's answer here at fetch time, so the pool build reads a fact, not a guess.

Additive: the columns default to false. Downgrade drops them.
"""

from __future__ import annotations

from alembic import op

revision: str = "0009_github_bots"
down_revision: str | None = "0008_hashed_emails"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("pull_requests", "review_comments", "issue_comments")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS author_is_bot boolean NOT NULL DEFAULT false"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS author_is_bot")
