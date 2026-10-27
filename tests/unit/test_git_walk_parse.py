"""The walker's log parser: every field the commit graph needs, and loud failure."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime

import pytest

from provenance.ingest.git_walk import (
    MalformedLogRecord,
    _parse_raw_line,
    _parse_raw_record,
    is_mechanical,
)
from provenance.privacy import EMAIL_RE, hash_email, scrub_emails

SHA = "1a" * 20
PARENT = "2b" * 20
TREE = "3c" * 20
OLD = "4d" * 20
NEW = "5e" * 20
ZERO = "0" * 40
WHEN = "2020-01-01T00:00:00+00:00"


def _block(*header: str, raw: str = "") -> str:
    return "\x1e" + "\x00".join(header) + "\x00" + raw


def test_every_header_field_reaches_the_record() -> None:
    record = _parse_raw_record(
        _block(
            SHA,
            PARENT,
            TREE,
            "Ada",
            "ada@example.com",
            "2020-01-02T03:04:05+00:00",
            "Grace",
            "grace@example.com",
            "2020-01-03T03:04:05+00:00",
            "subject line",
            "subject line\n\nbody\n",
            raw=f"\n:100644 100644 {OLD} {NEW} M\tpkg/a.py\n",
        )
    )
    assert (record.sha, record.parents, record.tree_sha) == (SHA, [PARENT], TREE)
    assert (record.author_name, record.committer_name) == ("Ada", "Grace")
    assert record.author_email_sha256 == hash_email("ada@example.com")
    assert record.committer_email_sha256 == hash_email("grace@example.com")
    assert record.author_time == datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert record.committer_time == datetime(2020, 1, 3, 3, 4, 5, tzinfo=UTC)
    assert record.message == "subject line\n\nbody\n"
    assert [change.new_path for change in record.changes] == ["pkg/a.py"]
    # walk_history numbers commits; the parser cannot know the total.
    assert record.topo_order is None


def test_a_root_commit_has_no_parents() -> None:
    record = _parse_raw_record(
        _block(SHA, "", TREE, "a", "a@x", WHEN, "a", "a@x", WHEN, "init", "init\n")
    )
    assert record.parents == []
    assert record.changes == []


def test_a_record_missing_header_fields_stops_the_walk() -> None:
    with pytest.raises(MalformedLogRecord, match="header fields"):
        _parse_raw_record(_block(SHA, PARENT, TREE))


@pytest.mark.parametrize(
    ("line", "old_path", "new_path", "old_blob", "new_blob", "similarity"),
    [
        (f":000000 100644 {ZERO} {NEW} A\tpkg/a.py", None, "pkg/a.py", ZERO, NEW, None),
        (f":100644 000000 {OLD} {ZERO} D\tpkg/a.py", "pkg/a.py", None, OLD, ZERO, None),
        (f":100644 100644 {OLD} {NEW} M\tpkg/a.py", "pkg/a.py", "pkg/a.py", OLD, NEW, None),
        (
            f":100644 100644 {OLD} {NEW} R087\tpkg/a.py\tpkg/b.py",
            "pkg/a.py",
            "pkg/b.py",
            OLD,
            NEW,
            87,
        ),
    ],
)
def test_raw_lines(line, old_path, new_path, old_blob, new_blob, similarity) -> None:
    change = _parse_raw_line(line)
    assert (change.old_path, change.new_path) == (old_path, new_path)
    assert (change.old_blob, change.new_blob, change.similarity) == (old_blob, new_blob, similarity)


def test_an_all_octal_blob_is_not_swallowed_by_the_modes() -> None:
    """The old regex let its `[0-7 ]+` modes group absorb a blob made of octal digits."""
    octal = "1234567" * 5 + "12345"
    change = _parse_raw_line(f":100644 100644 {octal} {NEW} M\tpkg/a.py")
    assert change.old_blob == octal


def test_an_unparseable_raw_line_stops_the_walk() -> None:
    with pytest.raises(MalformedLogRecord, match="--raw line"):
        _parse_raw_line(":100644 M\tpkg/a.py")


# ----------------------------------------------------------------- personal data


def test_the_parser_lets_no_address_through() -> None:
    """DATA-PROTECTION.md §2: the raw address never leaves the parser."""
    subject = "fix scheduler loop (#7)"
    message = f"{subject}\n\nCo-authored-by: Lin <lin@example.org>\n"
    record = _parse_raw_record(
        _block(
            SHA,
            PARENT,
            TREE,
            "Ada",
            "ada@example.com",
            WHEN,
            "Grace",
            "grace@example.com",
            WHEN,
            subject,
            message,
        )
    )
    assert record.message == scrub_emails(message)
    assert f"<email:{hash_email('lin@example.org')[:12]}>" in record.message
    values = [getattr(record, f.name) for f in fields(record)]
    assert not any(isinstance(v, str) and EMAIL_RE.search(v) for v in values)


def test_an_empty_address_is_stored_as_none() -> None:
    record = _parse_raw_record(
        _block(SHA, "", TREE, "a", "", WHEN, "a", " ", WHEN, "init", "init\n")
    )
    assert (record.author_email_sha256, record.committer_email_sha256) == (None, None)


@pytest.mark.parametrize(
    ("name", "email", "verdict"),
    [
        (
            "dependabot[bot]",
            "49699333+dependabot[bot]@users.noreply.github.com",
            (True, "bot-author"),
        ),
        # Decided by the address alone: classifying after hashing would call this a human.
        ("GitHub", "noreply@github.com", (True, "bot-author")),
        ("Ada", "ada@example.com", (False, None)),
    ],
)
def test_bot_verdicts_survive_hashing(
    name: str, email: str, verdict: tuple[bool, str | None]
) -> None:
    record = _parse_raw_record(
        _block(
            SHA,
            PARENT,
            TREE,
            name,
            email,
            WHEN,
            name,
            email,
            WHEN,
            "fix scheduler loop",
            "fix scheduler loop\n",
        )
    )
    assert is_mechanical(record.subject, bot_author=record.bot_author, sha=record.sha) == verdict
