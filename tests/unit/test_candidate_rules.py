"""ADR-0003 §3's question rules and the 2026-10-29 addendum's, on synthetic walks."""

from __future__ import annotations

from provenance.eval.candidates import ADDED, CHANGED, Event, SymbolWalk, apply_rules

A, B = 1, 2  # two file ids
PATHS = {A: "pkg/a.py", B: "pkg/b.py"}


def event(template, file_id, name, sha, *, month="2023-03", topo=1):
    return Event(template, file_id, PATHS[file_id], name, sha, topo, month)


def survivors(events, *, head, disappeared=None):
    walk = SymbolWalk(events=list(events), disappeared=disappeared or {}, head=head)
    return apply_rules(walk)


def test_a_symbol_gone_at_head_is_dropped():
    events = [
        event(ADDED, A, "alpha", "s1"),
        event(ADDED, A, "gone", "s1"),
        event(CHANGED, A, "gone", "s2"),
    ]
    kept, counts = survivors(events, head={A: frozenset({"alpha"})})
    assert [(e.template, e.qualified_name) for e in kept] == [(ADDED, "alpha")]
    assert (counts[ADDED]["at_head"], counts[CHANGED]["at_head"]) == (1, 0)


def test_a_short_name_leaving_another_file_in_the_same_commit_is_a_move():
    moved = event(ADDED, A, "run", "s1")
    kept, _ = survivors([moved], head={A: frozenset({"run"})}, disappeared={"s1": {(B, "run")}})
    assert kept == []


def test_a_move_within_one_file_is_still_an_addition():
    # `Old.run` left file A and `New.run` arrived in file A in one commit. No other file
    # is involved, so this is not a cross-file move.
    added = event(ADDED, A, "New.run", "s1")
    kept, _ = survivors([added], head={A: frozenset({"New.run"})}, disappeared={"s1": {(A, "run")}})
    assert kept == [added]


def test_the_name_leaving_another_file_in_a_different_commit_is_not_a_move():
    added = event(ADDED, A, "run", "s1")
    kept, _ = survivors([added], head={A: frozenset({"run"})}, disappeared={"s0": {(B, "run")}})
    assert kept == [added]


def test_a_symbol_added_twice_in_its_file_is_dropped_both_times():
    first, second = event(ADDED, A, "f", "s1", topo=1), event(ADDED, A, "f", "s3", topo=3)
    kept, counts = survivors([first, second], head={A: frozenset({"f"})})
    assert kept == []
    assert counts[ADDED] == {"raw": 2, "at_head": 2, "not_a_move": 2, "added_once": 0}


def test_two_qualifying_commits_in_one_month_drop_that_month_only():
    march = [
        event(CHANGED, A, "f", "s1", month="2023-03"),
        event(CHANGED, A, "f", "s2", month="2023-03"),
    ]
    april = event(CHANGED, A, "f", "s3", month="2023-04")
    kept, _ = survivors([*march, april], head={A: frozenset({"f"})})
    assert kept == [april]


def test_a_changed_name_in_two_head_files_is_dropped():
    changed = event(CHANGED, A, "setup", "s1")
    kept, _ = survivors([changed], head={A: frozenset({"setup"}), B: frozenset({"setup"})})
    assert kept == []


def test_an_added_question_names_its_file_so_a_shared_name_is_fine():
    added = event(ADDED, A, "setup", "s1")
    kept, _ = survivors([added], head={A: frozenset({"setup"}), B: frozenset({"setup"})})
    assert kept == [added]


def test_every_rule_is_counted_in_order():
    _, counts = survivors([], head={})
    assert counts == {
        ADDED: {"raw": 0, "at_head": 0, "not_a_move": 0, "added_once": 0},
        CHANGED: {"raw": 0, "at_head": 0, "one_commit_per_month": 0, "unique_name": 0},
    }
