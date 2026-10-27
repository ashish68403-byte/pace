"""Personal data: the only code allowed to see a raw email address.

DATA-PROTECTION.md §2 promises that an email address is never written to disk, the
database, the manifest or a log. Two ingestion paths read addresses -- the GitHub fetch
(`scripts/fetch_corpus.py`) and the git walk (`provenance/ingest/git_walk.py`) -- and
both redact through these functions, so an address hashes the same on either path and
"same author" stays a join key across them.

Checkpoint A's git walk once stored addresses raw, because these helpers lived only in
the fetch script: docs/tasks/ingest/004_personal_data.md.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

__all__ = ["EMAIL_RE", "hash_email", "scrub_emails"]

#: Deliberately broad. A false positive costs a redacted token in a message; a false
#: negative puts someone's email address in a corpus we then index and query.
EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def hash_email(email: str) -> str:
    """One-way, stable, and NOT reversible without the original address.

    Stable so that "same author across commits" is still a queryable join key; hashed so
    that the address itself never lands in the database, the manifest, or a backup.
    Lowercased first because git records mixed case and the same human would otherwise
    hash to two different identities.
    """
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]


def scrub_emails(text: str) -> str:
    """Replace every email address in free text with a stable opaque marker.

    Idempotent: a marker (``<email:0123456789ab>``) contains no ``@``, so scrubbing
    scrubbed text changes nothing. That is what lets migration 0008 re-run safely.
    """
    return EMAIL_RE.sub(lambda m: f"<email:{hash_email(m.group(0))[:12]}>", text)
