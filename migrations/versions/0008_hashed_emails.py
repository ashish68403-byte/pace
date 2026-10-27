"""0008_hashed_emails: no raw email address in `commits`, enforced by the schema.

Revision ID: 0008_hashed_emails
Revises: 0007_rationale_evidence

DATA-PROTECTION.md §2 says an email address is never written to the database.
Checkpoint A's git walk wrote them anyway -- author and committer addresses on every
commit, and unscrubbed subjects and messages -- because hash_email() and
scrub_emails() lived only in the GitHub fetch script (docs/tasks/ingest/004). This
revision:

1. renames commits.author_email / committer_email to *_email_sha256, so each column
   says what it may hold;
2. rewrites every stored value that is not already a hash, and scrubs every subject
   and message, with the SAME rules the parser now applies. They are frozen below,
   because a migration must do tomorrow what it does today;
   tests/unit/test_privacy.py pins the copy to provenance/privacy.py;
3. adds CHECK constraints, so the database refuses a raw address from now on, even
   one written by a future bug.

The project's first migration with a Python data step. It is a no-op on an empty
database (CI's round-trip) and safe to re-run: hashes are kept, and scrubbing
scrubbed text changes nothing.

Downgrade drops the CHECKs and renames the columns back. The values stay hashed and
scrubbed: no downgrade restores a raw address, by design.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0008_hashed_emails"
down_revision: str | None = "0007_rationale_evidence"
branch_labels: str | None = None
depends_on: str | None = None

# -- frozen copy of provenance/privacy.py at this revision (pinned by a unit test) --
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_HASH_RE = re.compile(r"[0-9a-f]{32}")


def _hash_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]


def _scrub_emails(text: str) -> str:
    return _EMAIL_RE.sub(lambda m: f"<email:{_hash_email(m.group(0))[:12]}>", text)


def _hashed(value: str | None) -> str | None:
    """Keep a hash, turn a blank into NULL, hash anything else."""
    if value is None or not value.strip():
        return None
    if _HASH_RE.fullmatch(value):
        return value
    return _hash_email(value)


#: The same pattern in Postgres's regex dialect, for the CHECK.
_EMAIL_SQL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE commits RENAME COLUMN author_email TO author_email_sha256;
        ALTER TABLE commits RENAME COLUMN committer_email TO committer_email_sha256;
        """
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT repo_id, commit_sha, author_email_sha256, committer_email_sha256, "
            "subject, message FROM commits"
        )
    ).fetchall()
    updates: list[dict[str, Any]] = []
    for repo_id, sha, author, committer, subject, message in rows:
        new = (_hashed(author), _hashed(committer), _scrub_emails(subject), _scrub_emails(message))
        if new != (author, committer, subject, message):
            updates.append(
                {
                    "repo_id": repo_id,
                    "sha": sha,
                    "author": new[0],
                    "committer": new[1],
                    "subject": new[2],
                    "message": new[3],
                }
            )
    if updates:
        bind.execute(
            sa.text(
                "UPDATE commits SET author_email_sha256 = :author, "
                "committer_email_sha256 = :committer, subject = :subject, message = :message "
                "WHERE repo_id = :repo_id AND commit_sha = :sha"
            ),
            updates,
        )

    op.execute(
        f"""
        ALTER TABLE commits ADD CONSTRAINT commits_author_email_hashed
            CHECK (author_email_sha256 IS NULL OR author_email_sha256 ~ '^[0-9a-f]{{32}}$');
        ALTER TABLE commits ADD CONSTRAINT commits_committer_email_hashed
            CHECK (committer_email_sha256 IS NULL OR committer_email_sha256 ~ '^[0-9a-f]{{32}}$');
        ALTER TABLE commits ADD CONSTRAINT commits_no_raw_email
            CHECK (subject !~ '{_EMAIL_SQL}' AND message !~ '{_EMAIL_SQL}');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE commits DROP CONSTRAINT IF EXISTS commits_no_raw_email;
        ALTER TABLE commits DROP CONSTRAINT IF EXISTS commits_committer_email_hashed;
        ALTER TABLE commits DROP CONSTRAINT IF EXISTS commits_author_email_hashed;
        ALTER TABLE commits RENAME COLUMN committer_email_sha256 TO committer_email;
        ALTER TABLE commits RENAME COLUMN author_email_sha256 TO author_email;
        """
    )
