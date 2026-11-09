"""Slice C2: reading git's blame and `git log -L` output, and merging lines into ranges."""

from __future__ import annotations

import pytest

from provenance.eval.runner import FULL_GOLDEN
from provenance.ingest.blame import (
    LineOwner,
    OwnerRange,
    blame_algo,
    golden_target,
    log_algo,
    owner_ranges,
    parse_line_log,
    parse_porcelain,
)

A = "a" * 40
B = "b" * 40
M = "c" * 40  # a mechanical commit

PORCELAIN = "\n".join(
    [
        f"{A} 1 1 2",
        "author First",
        "author-time 1",
        "summary first",
        "boundary",
        "filename pkg/old.py",
        "\tdef f():",
        f"{A} 2 2",
        "\t    return 1",
        f"{B} 5 3 1",
        "author Second",
        "summary second",
        f"previous {A} pkg/old.py",
        "filename src/new.py",
        "\tfilename looks like a header but is a line",
        f"{A} 3 4 1",
        "\t    y = 3",
        "",
    ]
)


def test_porcelain_gives_each_line_its_owner_and_where_it_was_written():
    assert parse_porcelain(PORCELAIN) == [
        LineOwner(1, A, "pkg/old.py", 1),
        LineOwner(2, A, "pkg/old.py", 2),
        LineOwner(3, B, "src/new.py", 5),
        LineOwner(4, A, "pkg/old.py", 3),  # A's path is remembered from its first appearance
    ]


def test_a_quoted_path_is_refused_rather_than_misread():
    with pytest.raises(ValueError, match="quotes a path"):
        parse_porcelain(f'{A} 1 1 1\nfilename "pkg/sp\\303\\251cial.py"\n\tx = 1\n')


def test_a_line_without_a_filename_is_refused():
    with pytest.raises(ValueError, match="no filename"):
        parse_porcelain(f"{A} 1 1 1\n\tx = 1\n")


def _owners(*rows: tuple[int, str, str, int]) -> list[LineOwner]:
    return [LineOwner(*row) for row in rows]


def test_consecutive_lines_with_both_owners_unchanged_make_one_range():
    plain = _owners((10, A, "p.py", 3), (11, A, "p.py", 4), (12, A, "p.py", 5))
    assert owner_ranges(plain, plain, mechanical={M}) == [OwnerRange(10, 13, A, "p.py", 3, A)]


@pytest.mark.parametrize(
    ("second", "second_filtered"),
    [
        ((11, B, "p.py", 4), (11, B, "p.py", 4)),  # the plain owner changes
        ((11, A, "q.py", 4), (11, A, "q.py", 4)),  # the path it was written at changes
        ((11, A, "p.py", 9), (11, A, "p.py", 9)),  # its original lines stop being consecutive
        ((12, A, "p.py", 4), (12, A, "p.py", 4)),  # a gap, as between two functions
        ((11, A, "p.py", 4), (11, B, "p.py", 4)),  # only the filtered owner changes
    ],
)
def test_a_range_ends_where_any_owner_fact_changes(second, second_filtered):
    first = LineOwner(10, A, "p.py", 3)
    plain, filtered = [first, LineOwner(*second)], [first, LineOwner(*second_filtered)]
    assert len(owner_ranges(plain, filtered, mechanical={M})) == 2


def test_a_filtered_owner_that_is_still_mechanical_is_stored_as_none():
    plain = _owners((5, M, "p.py", 5), (6, M, "p.py", 6))
    filtered = _owners((5, M, "p.py", 5), (6, A, "p.py", 2))
    assert owner_ranges(plain, filtered, mechanical={M}) == [
        OwnerRange(5, 6, M, "p.py", 5, None),
        OwnerRange(6, 7, M, "p.py", 6, A),
    ]


def test_the_two_passes_must_cover_the_same_lines():
    with pytest.raises(ValueError, match="different lines"):
        owner_ranges(_owners((1, A, "p.py", 1)), _owners((2, A, "p.py", 2)), mechanical={M})


def test_line_log_gives_each_listed_commit_once_newest_first():
    assert parse_line_log(f"{B}\n\n{A}\n{B}\nnot a commit\n") == [B, A]


def test_the_algorithm_strings_name_what_made_a_row():
    made = blame_algo("2.43.0", 2223, "f" * 64)
    assert "--first-parent" in made and "2223 mechanical commits" in made and "f" * 64 in made
    assert log_algo("2.43.0") == "git 2.43.0 log --first-parent --format=%H -s -L <span>:<path>"


def test_the_golden_target_is_the_retained_dev_records_functions():
    target = golden_target(FULL_GOLDEN)
    assert target.pinned == "1e2ad803f74eadb5fb28d4504ca06b008c48b63e"
    assert (target.questions, len(target.functions)) == (4206, 1777)
    assert len({path for path, _ in target.functions}) == 215
