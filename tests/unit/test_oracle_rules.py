"""Slice C3b: CodeShovel's oracle read as data, its labels projected, its functions resolved."""

from __future__ import annotations

import json

import pytest

from provenance.eval.oracle import (
    OracleFunction,
    OracleRefused,
    Resolved,
    change_kinds,
    project_labels,
    read_oracle,
    rename_or_move,
    resolve,
    scores,
)

SOURCE = (
    "import functools\n\n\n"
    "@functools.cache\n"
    "def top(x):\n"
    "    return x\n\n\n"
    "class Box:\n"
    "    def put(self, item):\n"
    "        self.item = item\n\n"
    "    @property\n"
    "    def size(self):\n"
    "        return 1\n\n"
    "    @size.setter\n"
    "    def size(self, value):\n"
    "        self._size = value\n\n\n"
    "def outer():\n"
    "    def inner():\n"
    "        return 2\n\n"
    "    return inner\n"
)


def test_oracle_files_are_read_as_data_in_name_order(tmp_path):
    for stem, line in (("b-two", 9), ("a-one", 3)):
        record = {
            "repositoryName": "proj",
            "filePath": "pkg/m.py",
            "functionName": "f",
            "functionStartLine": line,
            "startCommitName": "s" * 40,
            "expectedResult": {"c" * 40: "Yintroduced"},
        }
        (tmp_path / f"{stem}.json").write_text(json.dumps(record), encoding="utf-8")
    found = read_oracle(tmp_path.glob("*.json"))
    assert [function.oracle for function in found] == ["a-one", "b-two"]
    assert found[0] == OracleFunction(
        "a-one", "proj", "pkg/m.py", "f", 3, "s" * 40, {"c" * 40: "Yintroduced"}
    )


def _function(*labels: str) -> OracleFunction:
    return OracleFunction("o", "p", "f.py", "f", 1, "s", {str(i): k for i, k in enumerate(labels)})


def test_the_16_are_found_inside_a_multichange_too():
    assert change_kinds("Ymultichange(Yrename,Ybodychange)") == {
        "Ymultichange",
        "Yrename",
        "Ybodychange",
    }
    assert rename_or_move(_function("Yintroduced", "Ymultichange(Yrename,Ybodychange)"))
    assert rename_or_move(_function("Yintroduced", "Yfilerename"))
    assert rename_or_move(_function("Yintroduced", "Ymovefromfile"))
    assert not rename_or_move(_function("Yintroduced", "Ybodychange", "Ybodychagne"))


#: The first-parent line r - a - m - b; m merged the side commits x and y.
POSITION = {"r": 0, "a": 1, "m": 2, "b": 3}
DESCENDANTS = {"x": ["y", "b", "m"], "y": ["b", "m"], "z": []}


def test_labels_off_the_first_parent_line_move_to_their_merge():
    assert project_labels(["r", "a"], POSITION, DESCENDANTS.__getitem__) == {"r", "a"}
    assert project_labels(["x", "a"], POSITION, DESCENDANTS.__getitem__) == {"m", "a"}
    assert project_labels(["x", "y", "m"], POSITION, DESCENDANTS.__getitem__) == {"m"}


def test_a_label_reaching_no_first_parent_commit_is_refused():
    with pytest.raises(OracleRefused):
        project_labels(["z"], POSITION, DESCENDANTS.__getitem__)


def test_each_kind_of_function_resolves():
    assert resolve(SOURCE, "m.py", 5) == Resolved((4, 6), "top", "top-level", False)
    assert resolve(SOURCE, "m.py", 10) == Resolved((10, 11), "Box.put", "method", False)
    assert resolve(SOURCE, "m.py", 14) == Resolved((13, 15), "Box.size", "method", True)
    assert resolve(SOURCE, "m.py", 18) == Resolved((17, 19), "Box.size", "method", True)
    assert resolve(SOURCE, "m.py", 23) == Resolved((23, 24), "outer", "nested", False)


def test_a_line_where_no_function_starts_is_refused():
    with pytest.raises(OracleRefused):
        resolve(SOURCE, "m.py", 6)


def test_scores():
    assert scores({"a", "b", "c"}, {"a", "b"}) == pytest.approx(
        {"precision": 2 / 3, "recall": 1.0, "f1": 0.8}
    )
    assert scores(set(), {"a"}) == {"precision": 0.0, "recall": 0.0, "f1": 0.0}
