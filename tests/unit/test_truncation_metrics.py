"""The truncation metrics on hand-built sets: no git, no database."""

from __future__ import annotations

from provenance.analysis.truncation import Revision, aggregate, measure_file

TODAY = "core/x.py"


def rev(sha, topo, path, *, old=None, change="modified", subject="s"):
    return Revision(sha, topo, path, old, change, subject, "2020-01-01")


def test_hidden_share_is_the_rename_aware_history_the_current_path_misses():
    revisions = [
        rev("c3", 3, TODAY),
        rev("c2", 2, TODAY, old="x.py", change="renamed"),
        rev("c1", 1, "x.py", subject="add x (#7)"),
    ]
    m = measure_file(TODAY, current=["c3", "c2"], revisions=revisions, follow=["c3", "c2", "c1"])
    assert (m.n_current_path, m.n_rename_aware, m.n_hidden) == (2, 3, 1)
    assert m.hidden_share == 0.3333
    assert m.n_hidden_with_pr_reference == 1
    assert m.current_path_check


def test_disagreement_with_follow_is_reported_with_examples():
    m = measure_file(
        TODAY,
        current=["c2"],
        revisions=[rev("c2", 2, TODAY), rev("c1", 1, "old.py")],
        follow=["c2", "c0"],
    )
    assert (m.follow_precision, m.follow_recall, m.follow_f1) == (0.5, 0.5, 0.5)
    assert (m.only_in_graph, m.only_in_follow) == (("c1",), ("c0",))


def test_a_current_path_mismatch_fails_the_check():
    m = measure_file(TODAY, current=["c2", "c9"], revisions=[rev("c2", 2, TODAY)], follow=["c2"])
    assert not m.current_path_check
    assert m.current_path_mismatch == ("c9",)


def test_a_commit_is_hidden_everywhere_only_if_every_file_it_touches_hides_it():
    a = measure_file(
        "a.py",
        current=["c2"],
        revisions=[rev("c2", 2, "a.py"), rev("c1", 1, "old_a.py")],
        follow=[],
    )
    b = measure_file("b.py", current=["c1"], revisions=[rev("c1", 1, "b.py")], follow=[])
    agg = aggregate([a, b], subjects={"c1": "s", "c2": "s"})
    assert agg["commits_touching_scope"] == 2
    assert agg["commits_hidden_everywhere"] == 0  # c1 is hidden for a.py, visible for b.py


def test_pooled_and_per_file_shares():
    a = measure_file(
        "a.py", current=["c4"], revisions=[rev("c4", 4, "a.py"), rev("c3", 3, "o.py")], follow=[]
    )
    b = measure_file(
        "b.py", current=[], revisions=[rev("c2", 2, "p.py"), rev("c1", 1, "p.py")], follow=[]
    )
    agg = aggregate([a, b], subjects={})
    assert agg["pooled_hidden_share"] == 0.75  # 3 hidden of 4 (file, commit) pairs
    assert (agg["hidden_share_q1"], agg["hidden_share_median"], agg["hidden_share_q3"]) == (
        0.625,
        0.75,
        0.875,
    )
    assert agg["files_hiding_more_than_half"] == 1
