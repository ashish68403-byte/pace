"""Slice C4: the comparison on C3a's fixture repository, with hand-made questions."""

from __future__ import annotations

import json
import subprocess

import pytest
from lineage_fixture import SCOPE

from provenance.analysis import blame_census, lineage_census
from provenance.eval.comparison import (
    ComparisonRefused,
    compare,
    per_question_jsonl,
    render_json,
    run_comparison,
)
from provenance.eval.generator import pool_version
from provenance.eval.preregistration import coverage, selection
from provenance.ingest.blame import build as blame_build
from provenance.ingest.blame import golden_target
from provenance.lineage.build import build as lineage_build

pytestmark = pytest.mark.db

CORE, UTIL = SCOPE
#: qid: the query class, the function, the gold commit by name, then A, B, C, L and unfiltered
#: blame as the probe of the fixture's arms gives them.
QUESTIONS = {
    "q1-plus-c3": ("archaeology", (CORE, "plus"), "c3", (0.0, 0.25, 1.0, 1.0, 0.0)),
    "q2-plus-c11": ("archaeology", (CORE, "plus"), "c11", (1.0, 0.0, 0.0, 1.0, 1.0)),
    "q3-helper-c6": ("provenance", (UTIL, "helper"), "c6", (1.0, 1 / 3, 1.0, 1.0, 1.0)),
    "q4-helper-c1": ("provenance", (UTIL, "helper"), "c1", (0.0, 1 / 3, 1.0, 0.0, 0.0)),
    "q5-zero_b-c1": ("archaeology", (UTIL, "zero_b"), "c1", (1.0, 0.0, 0.0, 1.0, 1.0)),
    "q6-multiply-c5": ("provenance", (CORE, "multiply"), "c5", (0.0, 1 / 3, 1.0, 1.0, 0.0)),
    "q7-zero_a-c3": ("archaeology", (UTIL, "zero_a"), "c3", (0.0, 0.0, 0.0, 0.0, 0.0)),
}


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


def _write_audit(path, version, status="complete"):
    record = {
        "labels": {"status": status, "overall": {"rate": 0.9554, "ci95": [0.8488, 0.9879]}},
        "sample": {"pool_version": version},
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def _setup(repo, conn, repo_id, tmp_path):
    golden = tmp_path / "golden.jsonl"
    lines = []
    for qid, (query_class, (path, name), gold, _) in QUESTIONS.items():
        record = {
            "qid": qid,
            "query_class": query_class,
            "split": "dev",
            "question": f"Why did {name} change in this commit?",
            "required_evidence": {"commit_shas": [repo.sha[gold]]},
            "supporting_evidence": {"symbol_anchors": [{"path": path, "qualified_name": name}]},
            "generation_strategy": "handwritten_c4_fixture",
            "corpus_commit": repo.sha["c11"],
        }
        lines.append(json.dumps(record))
    golden.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target = golden_target(golden)
    blame_build(conn, repo=repo.root, repo_id=repo_id, target=target)
    lineage_build(conn, repo=repo.root, repo_id=repo_id)
    blame_arms, lineage = tmp_path / "blame_arms.json", tmp_path / "lineage.json"
    blame_arms.write_text(
        blame_census.render_json(
            blame_census.census(
                conn, repo=repo.root, repo_id=repo_id, target=target, move_commit=repo.sha["c7"]
            )
        ),
        encoding="utf-8",
    )
    lineage.write_text(
        lineage_census.render_json(
            lineage_census.census(
                conn,
                repo=repo.root,
                repo_id=repo_id,
                target=target,
                move_commit=repo.sha["c7"],
                group_size=2,
            )
        ),
        encoding="utf-8",
    )
    audit = tmp_path / "audit.json"
    _write_audit(audit, pool_version(conn, repo_id))
    root = tmp_path / "pace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "PREREGISTRATION.md").write_text("# Pre-registration\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pre-register")
    return {
        "repo": repo.root,
        "repo_id": repo_id,
        "golden": golden,
        "blame_arms": blame_arms,
        "lineage_results": lineage,
        "audit": audit,
        "root": root,
    }


def _compare(setup, conn):
    return compare(conn, **setup)


def test_each_questions_values(lineage_graph, writer_conn, test_repo_id, tmp_path):
    setup = _setup(lineage_graph, writer_conn, test_repo_id, tmp_path)
    _, rows = _compare(setup, writer_conn)
    assert [row["qid"] for row in rows] == sorted(QUESTIONS)
    for row in rows:
        expected = QUESTIONS[row["qid"]][3]
        found = (row["A"], row["B"], row["C"], row["L"], row["blame_unfiltered"])
        assert found == pytest.approx(expected), row["qid"]
    assert [row["size_c"] for row in rows] == [4, 4, 3, 3, 1, 3, 1]


def test_the_report(lineage_graph, writer_conn, test_repo_id, tmp_path):
    setup = _setup(lineage_graph, writer_conn, test_repo_id, tmp_path)
    found, _ = _compare(setup, writer_conn)
    recall = found["recall"]
    assert {arm: recall[arm]["all"]["point"] for arm in recall} == {
        "A": 0.4286,
        "B": 0.1786,
        "C": 0.5714,
        "L": 0.7143,
        "blame_unfiltered": 0.4286,
    }
    assert recall["C"]["by_class"]["provenance"]["point"] == 1.0
    assert recall["C"]["by_class"]["archaeology"]["point"] == 0.25
    differences = found["differences"]
    assert {name: differences[name]["difference"] for name in differences} == {
        "C-A": 0.1429,
        "B-A": -0.25,
        "C-B": 0.3929,
        "L-A": 0.2857,
        "blame_unfiltered-A": 0.0,
    }
    assert (differences["C-A"]["discordant"], differences["C-A"]["mde"]) == (5, 0.6261)
    assert (differences["L-A"]["discordant"], differences["L-A"]["mde"]) == (2, 0.396)
    assert differences["blame_unfiltered-A"]["mde"] is None
    assert "mde" not in differences["B-A"] and "mde" not in differences["C-B"]
    assert differences["C-A"]["over_validity"] == {
        "at_point": 0.1495,
        "at_upper": 0.1446,
        "at_lower": 0.1683,
    }
    claim_1, claim_2 = found["claim_1"], found["claim_2"]
    assert claim_1["verdict"] == coverage(claim_1["difference"], claim_1["interval"][0])
    assert claim_2["reading"] == selection(*claim_2["interval"])
    assert found["split"]["adds_up"] is True
    assert found["cross_check"]["agrees"] is True
    assert (found["inputs"]["questions"], found["inputs"]["functions"]) == (7, 5)
    assert found["validity"]["status"] == "current"
    sizes = found["arm_sizes"]
    assert (sizes["A"]["median"], sizes["A"]["max"]) == (1.0, 2)
    assert (sizes["C"]["median"], sizes["C"]["max"]) == (3.0, 4)
    assert (sizes["L"]["median"], sizes["L"]["max"]) == (2.0, 7)
    assert found["qualification"]["agreement_check"]["verdict"] in {"meets", "misses"}


def test_two_comparisons_render_the_same_bytes(lineage_graph, writer_conn, test_repo_id, tmp_path):
    setup = _setup(lineage_graph, writer_conn, test_repo_id, tmp_path)
    first, first_rows = _compare(setup, writer_conn)
    second, second_rows = _compare(setup, writer_conn)
    assert render_json(first) == render_json(second)
    assert per_question_jsonl(first_rows) == per_question_jsonl(second_rows)


def test_a_modified_tracked_file_refuses(lineage_graph, writer_conn, test_repo_id, tmp_path):
    setup = _setup(lineage_graph, writer_conn, test_repo_id, tmp_path)
    (setup["root"] / "docs" / "PREREGISTRATION.md").write_text("# edited\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="tracked files are modified"):
        _compare(setup, writer_conn)


def test_a_function_without_stored_git_log_l_refuses(
    lineage_graph, writer_conn, test_repo_id, tmp_path
):
    setup = _setup(lineage_graph, writer_conn, test_repo_id, tmp_path)
    writer_conn.execute(
        "DELETE FROM line_log_commits WHERE repo_id = %s AND qualified_name = 'zero_a'",
        (test_repo_id,),
    )
    with pytest.raises(ComparisonRefused, match="no stored blame or git log -L"):
        _compare(setup, writer_conn)


def test_a_missing_identity_or_lineage_build_refuses(
    lineage_graph, writer_conn, test_repo_id, tmp_path
):
    setup = _setup(lineage_graph, writer_conn, test_repo_id, tmp_path)
    writer_conn.execute(
        "DELETE FROM symbol_lineage WHERE repo_id = %s AND current_qualified_name = 'zero_a'",
        (test_repo_id,),
    )
    with pytest.raises(ComparisonRefused, match="no identity alive at the pin"):
        _compare(setup, writer_conn)
    writer_conn.execute("DELETE FROM symbol_lineage WHERE repo_id = %s", (test_repo_id,))
    with pytest.raises(ComparisonRefused, match="run `pace lineage build`"):
        _compare(setup, writer_conn)


def test_a_changed_census_summary_refuses(lineage_graph, writer_conn, test_repo_id, tmp_path):
    setup = _setup(lineage_graph, writer_conn, test_repo_id, tmp_path)
    census = json.loads(setup["blame_arms"].read_text(encoding="utf-8"))
    census["commits_per_function"]["arm_a"]["max"] += 1
    setup["blame_arms"].write_text(json.dumps(census), encoding="utf-8")
    with pytest.raises(ComparisonRefused, match="differ from the censuses"):
        _compare(setup, writer_conn)


def test_a_stale_or_incomplete_validity_refuses(lineage_graph, writer_conn, test_repo_id, tmp_path):
    setup = _setup(lineage_graph, writer_conn, test_repo_id, tmp_path)
    _write_audit(setup["audit"], "rationale-v1:another-pool")
    with pytest.raises(ComparisonRefused, match="validity is stale"):
        _compare(setup, writer_conn)
    _write_audit(setup["audit"], pool_version(writer_conn, test_repo_id), status="partial")
    with pytest.raises(ComparisonRefused, match="validity is incomplete"):
        _compare(setup, writer_conn)


def test_the_command_writes_nothing_without_the_ingested_repository(
    migrated_database, tmp_path, capsys
):
    out, per_question = tmp_path / "provenance.json", tmp_path / "provenance.jsonl"
    code = run_comparison(
        golden=tmp_path / "golden.jsonl",
        corpus=tmp_path,
        out=out,
        per_question_out=per_question,
    )
    assert code == 1
    assert not out.exists() and not per_question.exists()
    assert "nothing written" in capsys.readouterr().err
