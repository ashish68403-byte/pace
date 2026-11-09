"""Claim 3 on CodeShovel's Python oracle: lineage against git log -L, once (slice C3b).

docs/tasks/function-identity/003_oracle_run.md; ADR-0005 §8 and its addendum of 2026-11-09;
PREREGISTRATION.md claim 3 and its amendment of the same date. For each of the 40 functions:

* the oracle's commits are its labels, a label off the start commit's first-parent line moved
  to the merge that brought it in;
* lineage's are the versions of the identity whose span holds the function's `def` line at the
  start commit, followed across files over histories rebuilt in memory, with the engine C3a
  froze;
* `git log -L`'s are what `git log --first-parent -L` lists for the function's own lines.

Each arm gets precision, recall and F1 per function. Claim 3 is the paired mean F1 difference,
decided by `preregistration.identity`. The run writes nothing unless the code that runs is
committed, the oracle is CodeShovel's at its pinned commit, and every function resolves.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from provenance.eval.metrics import set_f1
from provenance.parse import parse_source
from provenance.parse.symbols import symbol_spans

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

#: The pre-registered 16 hold one of these, a Ymultichange's parts included.
RENAME_OR_MOVE: Final = frozenset({"Yrename", "Yfilerename", "Ymovefromfile"})
_KIND = re.compile(r"Y[a-z]+")


class OracleRefused(RuntimeError):
    """The run will not go ahead; nothing was written."""


@dataclass(frozen=True)
class OracleFunction:
    """One CodeShovel oracle file, read as data."""

    oracle: str
    project: str
    path: str
    name: str
    line: int
    start: str
    labels: Mapping[str, str]


def read_oracle(files: Iterable[Path]) -> list[OracleFunction]:
    """Each oracle file's function, in file-name order."""
    found: list[OracleFunction] = []
    for file in sorted(files):
        record = json.loads(file.read_text(encoding="utf-8"))
        found.append(
            OracleFunction(
                oracle=file.stem,
                project=str(record["repositoryName"]),
                path=str(record["filePath"]),
                name=str(record["functionName"]),
                line=int(record["functionStartLine"]),
                start=str(record["startCommitName"]),
                labels={str(sha): str(kind) for sha, kind in record["expectedResult"].items()},
            )
        )
    return found


def change_kinds(label: str) -> frozenset[str]:
    """`Ymultichange(Yrename,Ybodychange)` -> {Ymultichange, Yrename, Ybodychange}."""
    return frozenset(_KIND.findall(label))


def rename_or_move(function: OracleFunction) -> bool:
    """One of the pre-registered 16: a Yrename, Yfilerename or Ymovefromfile label."""
    return any(change_kinds(label) & RENAME_OR_MOVE for label in function.labels.values())


def project_labels(
    labels: Iterable[str],
    position: Mapping[str, int],
    descendants: Callable[[str], Iterable[str]],
) -> frozenset[str]:
    """Each label on the first-parent line as it is; any other at the merge that brought it in.

    `position` numbers the start commit's first-parent line from its root. `descendants(label)`
    is every commit that has the label as an ancestor and the start commit as a descendant.
    """
    projected: set[str] = set()
    for label in labels:
        if label in position:
            projected.add(label)
            continue
        on_line = [commit for commit in descendants(label) if commit in position]
        if not on_line:
            raise OracleRefused(f"label {label} reaches no commit on the first-parent line")
        projected.add(min(on_line, key=position.__getitem__))
    return frozenset(projected)


@dataclass(frozen=True)
class Resolved:
    """A function at its start commit: `git log -L`'s lines, and lineage's identity."""

    span: tuple[int, int]
    symbol: str
    kind: str
    property_part: bool


def _definition_at(root: Node, line: int) -> Node | None:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "function_definition" and node.start_point[0] + 1 == line:
            return node
        stack.extend(node.children)
    return None


def _kind(node: Node) -> str:
    parent = node.parent
    while parent is not None:
        if parent.type == "function_definition":
            return "nested"
        if parent.type == "class_definition":
            return "method"
        parent = parent.parent
    return "top-level"


def _property_part(whole: Node, data: bytes) -> bool:
    if whole.type != "decorated_definition":
        return False
    for child in whole.children:
        if child.type == "decorator":
            text = data[child.start_byte : child.end_byte].decode("utf-8", "replace").rstrip()
            if "property" in text or text.endswith((".setter", ".getter", ".deleter")):
                return True
    return False


def resolve(text: str, path: str, line: int) -> Resolved:
    """The definition starting at `line`, and the identity whose span holds it."""
    data = text.encode("utf-8")
    node = _definition_at(parse_source(data).root_node, line)
    if node is None:
        raise OracleRefused(f"{path}: no function definition starts at line {line}")
    parent = node.parent
    whole = parent if parent is not None and parent.type == "decorated_definition" else node
    spans = symbol_spans(text, path)
    if spans is None:
        raise OracleRefused(f"{path}: the chunker refuses the file at the start commit")
    holders = sorted(
        {
            (last - first, name)
            for name, parts in spans.items()
            for first, last in parts
            if first <= line <= last
        }
    )
    if not holders:
        raise OracleRefused(f"{path}: no function or method holds line {line}")
    if len(holders) > 1 and holders[0][0] == holders[1][0]:
        raise OracleRefused(f"{path}: {holders[0][1]} and {holders[1][1]} both hold line {line}")
    return Resolved(
        span=(whole.start_point[0] + 1, node.end_point[0] + 1),
        symbol=holders[0][1],
        kind=_kind(node),
        property_part=_property_part(whole, data),
    )


def scores(found: Collection[str], expected: Collection[str]) -> dict[str, float]:
    """Precision, recall and F1 of one commit set against the oracle's."""
    hits = len(set(found) & set(expected))
    return {
        "precision": hits / len(found) if found else 0.0,
        "recall": hits / len(expected) if expected else 0.0,
        "f1": set_f1(found, expected),
    }
