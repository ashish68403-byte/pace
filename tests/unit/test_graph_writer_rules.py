"""The pure rules that decide each file_revisions row."""

from __future__ import annotations

import pytest

from provenance.ingest.git_walk import FileChange
from provenance.ingest.graph_writer import one_change_per_file, path_at_commit, revision_row

OLD = "a1" * 20
NEW = "b2" * 20
ZERO = "0" * 40


def change(status, old_path, new_path, *, old_blob=OLD, new_blob=NEW, similarity=None):
    return FileChange(status, old_path, new_path, old_blob, new_blob, similarity)


def test_a_deletion_points_at_the_content_it_removed():
    row = revision_row(change("D", "pkg/a.py", None, new_blob=ZERO))
    assert row["blob_sha"] == OLD
    assert (row["change_type"], row["path_at_commit"], row["old_path"]) == (
        "deleted",
        "pkg/a.py",
        None,
    )


def test_a_rename_keeps_its_source_and_similarity():
    row = revision_row(change("R100", "pkg/a.py", "pkg/b.py", similarity=100))
    assert row == {
        "blob_sha": NEW,
        "change_type": "renamed",
        "old_path": "pkg/a.py",
        "path_at_commit": "pkg/b.py",
        "similarity": 100,
    }


def test_a_modification_has_no_old_path():
    row = revision_row(change("M", "pkg/a.py", "pkg/a.py"))
    assert (row["change_type"], row["old_path"], row["similarity"]) == ("modified", None, None)


def test_an_unknown_status_is_rejected():
    with pytest.raises(ValueError, match="unknown git status"):
        revision_row(change("X", "pkg/a.py", "pkg/a.py"))


def test_the_all_zero_placeholder_is_never_stored():
    with pytest.raises(ValueError, match="no usable blob"):
        revision_row(change("M", "pkg/a.py", "pkg/a.py", new_blob=ZERO))


def test_path_at_commit_uses_the_old_path_only_for_a_deletion():
    assert path_at_commit(change("D", "gone.py", None)) == "gone.py"
    assert path_at_commit(change("A", None, "new.py")) == "new.py"


def test_collapse_keeps_the_change_already_at_todays_path():
    older = change("D", "pkg/old.py", None)
    current = change("A", None, "core/pkg/a.py")
    kept, collapsed = one_change_per_file([(older, "core/pkg/a.py"), (current, "core/pkg/a.py")])
    assert kept == [(current, "core/pkg/a.py")]
    assert collapsed == 1


def test_collapse_falls_back_to_gits_order():
    first = change("M", "x.py", "x.py")
    second = change("M", "y.py", "y.py")
    kept, collapsed = one_change_per_file([(first, "z.py"), (second, "z.py")])
    assert kept == [(first, "z.py")]
    assert collapsed == 1


def test_distinct_files_are_left_alone():
    a, b = change("M", "a.py", "a.py"), change("M", "b.py", "b.py")
    assert one_change_per_file([(a, "a.py"), (b, "b.py")]) == ([(a, "a.py"), (b, "b.py")], 0)
