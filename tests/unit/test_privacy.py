"""Personal data: one implementation of the email rules, on every ingestion path."""

from __future__ import annotations

from pathlib import Path

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


def test_the_github_path_imports_the_shared_helpers() -> None:
    fetch = (ROOT / "scripts" / "fetch_corpus.py").read_text(encoding="utf-8")
    assert "from provenance.privacy import hash_email, scrub_emails" in fetch
    assert "def hash_email" not in fetch and "def scrub_emails" not in fetch
