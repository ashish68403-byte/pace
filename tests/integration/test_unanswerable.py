"""Slice D1: the unanswerable questions on C3a's fixture, with its lineage and its pool."""

from __future__ import annotations

import json
from collections import Counter

import pytest
from lineage_fixture import SCOPE

from provenance.eval.schema import load_golden_set, write_golden_set
from provenance.eval.unanswerable import (
    AFTER_THE_PIN,
    UnanswerableRefused,
    build,
    main,
    render_census,
)
from provenance.ingest.message_pool import write_pool
from provenance.lineage.build import build as lineage_build

pytestmark = pytest.mark.db

CORE, UTIL = SCOPE
#: (qid, split, function, a "changed" question's month or None for "added", the gold commit)
SOURCES = (
    ("d-plus", "dev", (CORE, "plus"), "February 2022", "c3"),
    ("d-multiply", "dev", (CORE, "multiply"), "February 2022", "c5"),
    ("d-helper", "dev", (UTIL, "helper"), None, "c6"),
    ("t-zero_a", "test", (UTIL, "zero_a"), None, "c9"),
    ("t-zero_b", "test", (UTIL, "zero_b"), "February 2022", "c9"),
)
COUNTS = {"dev": 2, "test": 1}
SEEN = "SELECT DISTINCT qualified_name FROM symbol_versions WHERE repo_id = %s"
MENTIONS = (
    "SELECT count(*) FROM chunks WHERE repo_id = %s AND strpos(lower(content), lower(%s)) > 0"
)


def _golden(path, repo):
    """The five retained questions above, worded as `pace golden generate` words them."""
    lines = []
    for qid, split, (where, name), month, gold in SOURCES:
        question = (
            f"Why did `{name}` change in {month}?"
            if month
            else f"Why was `{name}` added to `{where}`?"
        )
        record = {
            "qid": qid,
            "query_class": "archaeology" if month else "provenance",
            "split": split,
            "question": question,
            "required_evidence": {"commit_shas": [repo.sha[gold]]},
            "supporting_evidence": {"symbol_anchors": [{"path": where, "qualified_name": name}]},
            "generation_strategy": "handwritten_d1_fixture",
            "corpus_commit": repo.sha["c11"],
        }
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def ready(lineage_graph, writer_conn, test_repo_id, migrated_database, tmp_path):
    """C3a's fixture with its lineage and its commit-message pool, and the five questions."""
    lineage_build(writer_conn, repo=lineage_graph.root, repo_id=test_repo_id)
    write_pool(migrated_database, test_repo_id, numbering_starts=lineage_graph.sha["c1"])
    return _golden(tmp_path / "golden.jsonl", lineage_graph)


def test_each_kind_on_the_fixture(ready, lineage_graph, writer_conn, test_repo_id, query):
    negatives, census = build(writer_conn, repo_id=test_repo_id, golden=ready, counts=COUNTS)
    assert Counter((r.split, r.qid[:2]) for r in negatives) == {
        ("dev", "s3"): 2,
        ("dev", "s4"): 2,
        ("dev", "s5"): 2,
        ("test", "s3"): 1,
        ("test", "s4"): 1,
        ("test", "s5"): 1,
    }
    sources = {record.qid: record for record in load_golden_set(ready)}
    seen = {name.rpartition(".")[2] for (name,) in query(SEEN, test_repo_id)}
    for negative in negatives:
        source = sources[negative.qid[3:]]
        function = source.supporting_evidence.symbol_anchors[0]
        assert (negative.split, negative.corpus_commit) == (source.split, source.corpus_commit)
        if negative.qid.startswith("s3-"):
            assert negative.question == source.question
            assert negative.held_out == source.required_evidence
            assert negative.supporting_evidence.symbol_anchors == [function]
        elif negative.qid.startswith("s4-"):
            name = function.qualified_name
            assert negative.question in {f"Why did `{name}` change in {m}?" for m in AFTER_THE_PIN}
            assert negative.supporting_evidence.symbol_anchors == [function]
        else:
            short = negative.question.split("`")[1]
            assert short not in seen and query(MENTIONS, test_repo_id, short) == [(0,)]
            anchors = negative.supporting_evidence.symbol_anchors
            assert [(a.path, a.qualified_name) for a in anchors] == [(function.path, None)]
    assert census["inputs"]["pinned"] == lineage_graph.sha["c11"]
    assert census["inputs"]["pool_version"] == "rationale-v1:commit=11"
    assert census["inputs"]["lineage"] == {"names_seen": 8, "alive": 5}
    assert [census["splits"][split]["retained"] for split in ("dev", "test")] == [3, 2]
    assert census["splits"]["dev"]["s5"]["count"] == 2


def test_a_name_lineage_has_seen_is_not_asked_about(ready, writer_conn, test_repo_id):
    # Test's second question can splice only `zero_helper`, which the first took, or its
    # sibling's name, which lineage has seen: test reaches one S5 question of two.
    with pytest.raises(UnanswerableRefused, match="test s5: only 1 of 2 questions"):
        build(writer_conn, repo_id=test_repo_id, golden=ready, counts={"dev": 2, "test": 2})


def test_a_name_the_pool_mentions_is_not_asked_about(
    ready, writer_conn, test_repo_id, migrated_database
):
    counts = {"dev": 3, "test": 1}
    negatives, _ = build(writer_conn, repo_id=test_repo_id, golden=ready, counts=counts)
    assert "Why did `plus_multiply` change in February 2022?" in {r.question for r in negatives}
    migrated_database.execute(
        "UPDATE chunks SET content = content || ' (see Plus_Multiply)' "
        "WHERE repo_id = %s AND content = 'restore core'",
        (test_repo_id,),
    )
    # `plus` has one partner, so its one name is gone, and case does not hide it
    with pytest.raises(UnanswerableRefused, match="dev s5: only 2 of 3 questions"):
        build(writer_conn, repo_id=test_repo_id, golden=ready, counts=counts)


def test_before_a_lineage_build_nothing_is_built(
    lineage_graph, writer_conn, test_repo_id, tmp_path
):
    golden = _golden(tmp_path / "golden.jsonl", lineage_graph)
    with pytest.raises(UnanswerableRefused, match="run `pace lineage build`"):
        build(writer_conn, repo_id=test_repo_id, golden=golden, counts=COUNTS)


def test_a_golden_set_without_a_retained_question_builds_nothing(
    writer_conn, test_repo_id, tmp_path
):
    golden = tmp_path / "golden.jsonl"
    golden.write_text("# no questions\n", encoding="utf-8")
    with pytest.raises(UnanswerableRefused, match="no retained answerable question"):
        build(writer_conn, repo_id=test_repo_id, golden=golden, counts=COUNTS)


def test_two_builds_write_the_same_bytes_and_leave_the_golden_set_alone(
    ready, writer_conn, test_repo_id, tmp_path
):
    before = ready.read_bytes()
    written = []
    for run in ("first", "second"):
        negatives, census = build(writer_conn, repo_id=test_repo_id, golden=ready, counts=COUNTS)
        out = write_golden_set(negatives, tmp_path / f"{run}.jsonl")
        written.append((out.read_bytes(), render_census(census)))
    assert written[0] == written[1]
    assert ready.read_bytes() == before
    qids = [record.qid for record in load_golden_set(tmp_path / "first.jsonl")]
    assert qids == sorted(qids) and len(qids) == 9


def test_the_command_writes_nothing_without_the_ingested_repository(
    migrated_database, tmp_path, capsys
):
    out, census = tmp_path / "unanswerable.jsonl", tmp_path / "census.json"
    golden = tmp_path / "golden.jsonl"
    code = main(["--golden", str(golden), "--out", str(out), "--census", str(census)])
    assert code == 1
    assert not out.exists() and not census.exists()
    assert "nothing written" in capsys.readouterr().err
