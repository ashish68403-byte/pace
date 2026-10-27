"""Slice B2's pure rules: which PR a subject names, which links survive, what a document is."""

from __future__ import annotations

import hashlib

import pytest

from provenance.graph.ids import chunk_id
from provenance.ingest.message_pool import (
    DROPPED_BEFORE_NUMBERING,
    DROPPED_IMPLAUSIBLE,
    LINKED,
    NO_REFERENCE,
    PoolCommit,
    link_decisions,
    message_document,
    pr_reference,
)
from provenance.parse.chunker import lexical_tokens

SHA = "ab" * 20


@pytest.mark.parametrize(
    ("subject", "pr"),
    [
        ("Add uvicorn_logging_level config option (#55405)", 55405),
        ('Revert "Handle Example dags case (#41856)" (#42193)', 42193),
        ("Add Python 3.12 support (#36755) (#38025)", 38025),
        ("Merge pull request #10 from airbnb/fix_authenticate", 10),
        ("Fix (#12) handling of X", None),
        ("see #12 for context", None),
        ("rename a to alpha", None),
    ],
)
def test_the_subject_names_the_pr_the_way_github_wrote_it(subject: str, pr: int | None) -> None:
    assert pr_reference(subject) == pr


def _walk(*subjects: str) -> list[PoolCommit]:
    """Commits in walk order: topo_order 1, 2, 3, ..."""
    return [PoolCommit(f"{i:040x}", i, s, s) for i, s in enumerate(subjects, start=1)]


def test_links_before_the_numbering_boundary_are_dropped() -> None:
    walk = _walk("Merge pull request #10 from x/a", "Merge pull request #5 from y/b", "fix (#7)")
    outcomes = [d.outcome for d in link_decisions(walk, numbering_starts_at=2)]
    assert outcomes == [DROPPED_BEFORE_NUMBERING, LINKED, LINKED]


def test_a_number_far_below_the_running_maximum_is_dropped() -> None:
    walk = _walk("a (#3000)", "b (#3150)", "cleanup (#24)", "c (#3200)")
    decisions = link_decisions(walk, numbering_starts_at=1)
    assert [d.outcome for d in decisions] == [LINKED, LINKED, DROPPED_IMPLAUSIBLE, LINKED]
    assert decisions[2].pr == 24


def test_exactly_ten_percent_is_kept() -> None:
    walk = _walk("a (#1000)", "b (#100)", "c (#99)")
    outcomes = [d.outcome for d in link_decisions(walk, numbering_starts_at=1)]
    assert outcomes == [LINKED, LINKED, DROPPED_IMPLAUSIBLE]


def test_dropped_numbers_never_raise_the_running_maximum() -> None:
    # If #9000 counted, #40 would fall below its 10% floor; it precedes the boundary.
    walk = _walk("Merge pull request #9000 from x/a", "a (#40)", "b (#50)")
    outcomes = [d.outcome for d in link_decisions(walk, numbering_starts_at=2)]
    assert outcomes == [DROPPED_BEFORE_NUMBERING, LINKED, LINKED]


def test_a_commit_without_a_reference_is_not_linked() -> None:
    assert link_decisions(_walk("rename a to alpha"), numbering_starts_at=1)[0].outcome == (
        NO_REFERENCE
    )


def test_a_document_is_built_the_same_way_every_time() -> None:
    message = "fix scheduler loop (#7)\n\nCall _find_zombies once per loop.\n"
    content = "fix scheduler loop (#7)\n\nCall _find_zombies once per loop."
    doc = message_document(SHA, message, repo_slug="apache/airflow")
    assert (doc.path, doc.content, doc.end_line) == (f"commit/{SHA}", content, 3)
    assert doc.chunk_id == chunk_id("apache/airflow", f"commit/{SHA}", None, content)
    assert doc.content_sha == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert list(doc.lexical_tokens) == lexical_tokens(content)
    assert "findzombies" in doc.lexical_tokens  # the joined form BM25's tokenizer destroys
    assert message_document(SHA, message, repo_slug="apache/airflow") == doc
