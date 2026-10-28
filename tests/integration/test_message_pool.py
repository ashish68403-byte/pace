"""Slice B2 against pace-db: the commit-message pool on the fixture repository.

history_repo (see conftest) has nine first-parent commits; eight touch an in-scope file
(c4 only edits the README). Four of those carry a PR number: c2 (#11), m5 (#12),
c6 (#13), c7 (#14).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from provenance.ingest.message_pool import (
    DROPPED_BEFORE_NUMBERING,
    LINKED,
    NO_REFERENCE,
    PoolInvariantError,
    message_document,
    write_pool,
)

pytestmark = pytest.mark.db

POOL = ("c1", "c2", "c3", "m5", "c6", "c7", "c8", "c9")
LINKS = {"c2": "11", "m5": "12", "c6": "13", "c7": "14"}


def _documents(query, repo_id):
    return query(
        "SELECT path, chunk_id FROM chunks WHERE repo_id = %s AND kind = 'commit' ORDER BY path",
        repo_id,
    )


def _evidence(query, repo_id):
    return set(
        query(
            "SELECT c.path, e.evidence_kind::text, e.evidence_key, e.source "
            "FROM chunk_evidence e JOIN chunks c USING (chunk_id) WHERE e.repo_id = %s",
            repo_id,
        )
    )


def test_the_pool_is_written_with_its_evidence(
    history_repo, test_repo_id, ingest, migrated_database, query
):
    ingest()
    sha = history_repo.sha
    report = write_pool(migrated_database, test_repo_id, numbering_starts=sha["c1"])
    paths = [path for path, _ in _documents(query, test_repo_id)]
    assert paths == sorted(f"commit/{sha[name]}" for name in POOL)
    expected = {(f"commit/{sha[n]}", "commit", sha[n], "self") for n in POOL}
    expected |= {(f"commit/{sha[n]}", "pull_request", pr, "linked") for n, pr in LINKS.items()}
    assert _evidence(query, test_repo_id) == expected
    assert (report.documents_inserted, report.evidence_inserted) == (8, 12)
    assert report.outcomes == {LINKED: 4, NO_REFERENCE: 4}


def test_the_numbering_boundary_drops_earlier_links(
    history_repo, test_repo_id, ingest, migrated_database, query
):
    ingest()
    report = write_pool(migrated_database, test_repo_id, numbering_starts=history_repo.sha["c6"])
    linked = {key for _, _, key, source in _evidence(query, test_repo_id) if source == "linked"}
    assert linked == {"13", "14"}
    assert report.outcomes == {LINKED: 2, DROPPED_BEFORE_NUMBERING: 2, NO_REFERENCE: 4}


def test_a_second_build_writes_nothing(history_repo, test_repo_id, ingest, migrated_database):
    ingest()
    boundary = history_repo.sha["c1"]
    first = write_pool(migrated_database, test_repo_id, numbering_starts=boundary)
    second = write_pool(migrated_database, test_repo_id, numbering_starts=boundary)
    assert (second.documents_inserted, second.evidence_inserted) == (0, 0)
    assert (second.documents_skipped, second.evidence_skipped) == (
        first.documents_inserted,
        first.evidence_inserted,
    )


def test_a_stale_document_fails_the_build_and_writes_nothing(
    history_repo, test_repo_id, ingest, migrated_database, query
):
    ingest()
    sha = history_repo.sha
    stale = message_document(sha["c7"], "an older text of this message", repo_slug="pace-test/x")
    migrated_database.execute(
        "INSERT INTO chunks (chunk_id, repo_id, path, kind, language, start_line, end_line, "
        "content, content_sha, chunking_strategy_version, first_seen_commit, last_seen_commit) "
        "VALUES (%s, %s, %s, 'commit', 'text', 1, 1, %s, %s, 'commit-message-v0', %s, %s)",
        (
            stale.chunk_id,
            test_repo_id,
            stale.path,
            stale.content,
            stale.content_sha,
            sha["c7"],
            sha["c7"],
        ),
    )
    with pytest.raises(PoolInvariantError, match=sha["c7"]):
        write_pool(migrated_database, test_repo_id, numbering_starts=sha["c1"])
    assert _documents(query, test_repo_id) == [(stale.path, stale.chunk_id)]
    assert _evidence(query, test_repo_id) == set()


def test_a_missing_boundary_fails_the_build_and_writes_nothing(
    test_repo_id, ingest, migrated_database, query
):
    ingest()
    with pytest.raises(PoolInvariantError, match="numbering boundary"):
        write_pool(migrated_database, test_repo_id, numbering_starts="f" * 40)
    assert _documents(query, test_repo_id) == []


def test_the_resolver_finds_the_commit_and_its_pr_in_one_document(
    history_repo, test_repo_id, ingest, migrated_database, query
):
    from provenance.eval.anchors import resolve_anchors
    from provenance.eval.schema import GoldenRecord

    ingest()
    sha = history_repo.sha
    write_pool(migrated_database, test_repo_id, numbering_starts=sha["c1"])
    [(document,)] = query(
        "SELECT chunk_id FROM chunks WHERE repo_id = %s AND path = %s",
        test_repo_id,
        f"commit/{sha['c7']}",
    )
    record = GoldenRecord.model_validate(
        {
            "qid": "b2-1",
            "query_class": "provenance",
            "split": "dev",
            "question": "Why did `alpha` change after the move?",
            "required_evidence": {"commit_shas": [sha["c7"]], "pr_numbers": [14]},
            "generation_strategy": "handwritten_test",
        }
    )
    resolved = resolve_anchors(record, migrated_database, repo_id=test_repo_id)
    assert resolved.required_units == {
        ("commit", sha["c7"]): {document},
        ("pr", "14"): {document},
    }
    assert resolved.unresolved_required == set()


def _leakage_probe():
    """scripts/ is not a package: load the probe the way `pace leakage` does."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "leakage_probe.py"
    spec = importlib.util.spec_from_file_location("leakage_probe_for_b2", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


def test_the_probe_pools_rationale_text_only(history_repo, test_repo_id, ingest, migrated_database):
    ingest()
    sha = history_repo.sha
    write_pool(migrated_database, test_repo_id, numbering_starts=sha["c1"])
    migrated_database.execute(  # a stand-in for checkpoint C's code chunks
        "INSERT INTO chunks (chunk_id, repo_id, path, qualified_name, kind, start_line, end_line, "
        "content, content_sha, chunking_strategy_version, first_seen_commit, last_seen_commit) "
        "VALUES (%s, %s, 'core/src/pkg/alpha.py', 'alpha', 'chunk', 1, 2, %s, %s, 'v1', %s, %s)",
        ("c" * 64, test_repo_id, "def alpha():\n    return 3", "d" * 64, sha["c7"], sha["c7"]),
    )
    pool = _leakage_probe().load_pool_from_db(migrated_database, test_repo_id)
    assert len(pool) == 8
    assert all(document.path.startswith("commit/") for document in pool)


def test_the_census_counts_what_was_written(
    history_repo, test_repo_id, ingest, migrated_database, query
):
    from provenance.analysis.pool_census import build_census

    ingest()
    sha = history_repo.sha
    write_pool(migrated_database, test_repo_id, numbering_starts=sha["c1"])
    census = build_census(
        migrated_database, test_repo_id, history_repo.root, numbering_starts=sha["c1"]
    )
    [(mechanical,)] = query(
        "SELECT count(*) FROM commits WHERE repo_id = %s AND is_mechanical "
        "AND commit_sha = ANY(%s)",
        test_repo_id,
        [sha[name] for name in POOL],
    )
    assert census["pool"] == {"documents": 8, "merges": 1, "mechanical": mechanical}
    assert (census["links"]["linked"], census["links"]["no_reference"]) == (4, 4)
    assert census["evidence"] == {
        "self": 8,
        "linked": 4,
        "distinct_prs": 4,
        "prs_with_two_or_more_documents": 0,
    }
    assert census["corpus_commit"] == sha["c9"]


def test_the_census_refuses_when_stored_links_disagree_with_the_rule(
    history_repo, test_repo_id, ingest, migrated_database
):
    from provenance.analysis.pool_census import CensusCheckFailed, build_census

    ingest()
    sha = history_repo.sha
    write_pool(migrated_database, test_repo_id, numbering_starts=sha["c1"])
    with pytest.raises(CensusCheckFailed, match="0 missing, 2 unexpected"):
        build_census(migrated_database, test_repo_id, history_repo.root, numbering_starts=sha["c6"])
