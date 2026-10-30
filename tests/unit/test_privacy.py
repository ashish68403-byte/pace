"""Personal data: one implementation of the email rules, on every ingestion path."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from provenance.privacy import EMAIL_RE, hash_email, scrub_emails

ROOT = Path(__file__).resolve().parents[2]


def test_hash_email_is_stable_case_blind_and_32_hex() -> None:
    digest = hash_email("ada@example.com")
    assert digest == hash_email("  Ada@Example.COM ")
    assert len(digest) == 32 and set(digest) <= set("0123456789abcdef")
    assert digest != hash_email("grace@example.com")


def test_scrub_replaces_every_address_with_a_marker() -> None:
    text = "fix (#1)\n\nCo-authored-by: Lin <lin@example.org>\nSigned-off-by: a.b+c@d-e.co.uk\n"
    scrubbed = scrub_emails(text)
    assert EMAIL_RE.search(scrubbed) is None
    assert f"<email:{hash_email('lin@example.org')[:12]}>" in scrubbed
    assert scrubbed.startswith("fix (#1)\n\nCo-authored-by: Lin <")


def test_scrubbing_is_idempotent() -> None:
    once = scrub_emails("mail bob@example.net and ann@example.org")
    assert scrub_emails(once) == once


def test_text_without_an_address_is_untouched() -> None:
    # Decorators and handles are not addresses: no dot-separated domain after the @.
    text = "Use `foo@bar` decorators, not @property (#123)"
    assert scrub_emails(text) == text


def test_the_github_path_scrubs_through_the_shared_helper() -> None:
    fetch = (ROOT / "provenance" / "ingest" / "github_backfill.py").read_text(encoding="utf-8")
    assert "from provenance.privacy import scrub_emails" in fetch
    assert "def scrub_emails" not in fetch and "EMAIL_RE" not in fetch
    assert not (ROOT / "scripts" / "fetch_corpus.py").exists()


# ------------------------------------------------- migration 0008's frozen copy

SAMPLE_ADDRESSES = [
    "ada@example.com",
    "  Ada@Example.COM ",
    "49699333+dependabot[bot]@users.noreply.github.com",
]
SAMPLE_TEXTS = [
    "Co-authored-by: Lin <lin@example.org>",
    "no address here (#12)",
    "two: a@b.co and c.d@e-f.org",
]


def _migration_0008() -> ModuleType:
    path = ROOT / "migrations" / "versions" / "0008_hashed_emails.py"
    spec = importlib.util.spec_from_file_location("migration_0008", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0008_froze_the_same_rules() -> None:
    """A migration must not change behaviour when the package does, so 0008 carries its
    own copy. This test is what keeps the copy honest at the revision it was written."""
    m = _migration_0008()
    assert m._EMAIL_RE.pattern == EMAIL_RE.pattern
    assert [m._hash_email(a) for a in SAMPLE_ADDRESSES] == [hash_email(a) for a in SAMPLE_ADDRESSES]
    assert [m._scrub_emails(t) for t in SAMPLE_TEXTS] == [scrub_emails(t) for t in SAMPLE_TEXTS]


def test_migration_0008_keeps_hashes_and_turns_blanks_into_null() -> None:
    m = _migration_0008()
    digest = hash_email("ada@example.com")
    assert m._hashed(digest) == digest
    assert m._hashed("ada@example.com") == digest
    assert m._hashed("") is None and m._hashed("  ") is None and m._hashed(None) is None
