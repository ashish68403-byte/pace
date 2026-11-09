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

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from provenance.eval.metrics import paired_bootstrap, set_f1
from provenance.eval.preregistration import IDENTITY_MIN_F1_GAIN, identity
from provenance.ingest.blame import git_version, line_log, log_algo, show
from provenance.ingest.git_walk import CatFileBatch
from provenance.lineage.build import DETECTOR, Cache
from provenance.lineage.history import read_history, revisions_reader
from provenance.lineage.scope import follow_chain
from provenance.parse import parse_source
from provenance.parse.symbols import symbol_spans

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

#: The pre-registered 16 hold one of these, a Ymultichange's parts included.
RENAME_OR_MOVE: Final = frozenset({"Yrename", "Yfilerename", "Ymovefromfile"})
ARMS: Final = ("lineage", "git_log_l")
_KIND = re.compile(r"Y[a-z]+")
CODESHOVEL_COMMIT: Final = "0d29f498cc619078aaac61aa6132a153664fe123"
ORACLE_FILES: Final = 40
ORACLE_DIR: Final = Path("src") / "test" / "resources" / "oracles" / "python"
PREREGISTRATION: Final = "docs/PREREGISTRATION.md"
DEFAULT_CODESHOVEL: Final = Path.home() / "corpus" / "codeshovel"
DEFAULT_CLONES: Final = Path.home() / "corpus" / "oracle"
DEFAULT_OUT: Final = Path("results") / "oracle.json"
#: This repository: the code that runs, and the pre-registration it runs under.
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]


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


@dataclass(frozen=True)
class Scored:
    """One function's three commit sets."""

    function: OracleFunction
    resolved: Resolved
    oracle: frozenset[str]
    projected: int
    lineage: frozenset[str]
    git_log: frozenset[str]
    files_walked: int
    rounds: int


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """git in `repo` with global and system config switched off, as `blame._git` runs it."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def _out(repo: Path, *args: str) -> str:
    done = _git(repo, *args)
    if done.returncode != 0:
        raise OracleRefused(f"git {args[0]} failed in {repo}: {done.stderr.strip()[:300]}")
    return done.stdout


def first_parent_positions(repo: Path, start: str) -> dict[str, int]:
    """The start commit's first-parent line, numbered from its root."""
    line = _out(repo, "rev-list", "--first-parent", "--reverse", start).split()
    return {commit: index for index, commit in enumerate(line)}


def _descendants(repo: Path, start: str, label: str) -> list[str]:
    return _out(repo, "rev-list", "--ancestry-path", f"{label}..{start}").split()


def evaluate(
    functions: Sequence[OracleFunction], clones: Path, *, progress: bool = False
) -> list[Scored]:
    """Every function's three commit sets; one history walk per project and start commit."""
    cache: Cache = {}
    groups: dict[tuple[str, str], list[OracleFunction]] = {}
    for function in functions:
        groups.setdefault((function.project, function.start), []).append(function)
    scored: list[Scored] = []
    for (project, start), members in sorted(groups.items()):
        repo = clones / f"{project}.git"
        history = read_history(repo, start)
        position = first_parent_positions(repo, start)
        descendants = partial(_descendants, repo, start)
        with CatFileBatch(repo) as cat:
            read = revisions_reader(history, cat, cache)
            for function in members:
                if function.path not in history.files:
                    raise OracleRefused(f"{function.oracle}: {function.path} has no history")
                text = show(repo, start, function.path)
                resolved = resolve(text, function.path, function.line)
                followed = follow_chain(
                    function.path, resolved.symbol, read, history.python_touched
                )
                scored.append(
                    Scored(
                        function=function,
                        resolved=resolved,
                        oracle=project_labels(function.labels, position, descendants),
                        projected=sum(1 for label in function.labels if label not in position),
                        lineage=frozenset(v.commit for v in followed.lineage.versions),
                        git_log=frozenset(line_log(repo, start, function.path, [resolved.span])),
                        files_walked=len(followed.files),
                        rounds=followed.rounds,
                    )
                )
                if progress:
                    print(
                        f"oracle: {function.oracle}: {len(followed.files)} files walked in "
                        f"{followed.rounds} rounds",
                        file=sys.stderr,
                    )
    return sorted(scored, key=lambda item: item.function.oracle)


def _arm(item: Scored, arm: str) -> frozenset[str]:
    return item.lineage if arm == "lineage" else item.git_log


def _means(items: Sequence[Scored]) -> dict[str, Any]:
    found: dict[str, Any] = {"n": len(items)}
    for arm in ARMS:
        rows = [scores(_arm(item, arm), item.oracle) for item in items]
        found[arm] = {
            metric: round(sum(row[metric] for row in rows) / len(rows), 4) if rows else 0.0
            for metric in ("precision", "recall", "f1")
        }
    return found


def _row(item: Scored) -> dict[str, Any]:
    function, resolved = item.function, item.resolved
    row: dict[str, Any] = {
        "oracle": function.oracle,
        "project": function.project,
        "path": function.path,
        "name": function.name,
        "line": function.line,
        "start": function.start,
        "symbol": resolved.symbol,
        "span": list(resolved.span),
        "kind": resolved.kind,
        "property_part": resolved.property_part,
        "rename_or_move": rename_or_move(function),
        "files_walked": item.files_walked,
        "rounds": item.rounds,
        "projected_labels": item.projected,
        "oracle_commits": sorted(item.oracle),
    }
    for arm in ARMS:
        found = _arm(item, arm)
        entry: dict[str, Any] = {"commits": sorted(found)}
        for metric, value in scores(found, item.oracle).items():
            entry[metric] = round(value, 4)
        row[arm] = entry
    return row


def report(scored: Sequence[Scored], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The run's results: per function, per arm and group, and claim 3's verdict."""
    lineage_f1 = [scores(item.lineage, item.oracle)["f1"] for item in scored]
    git_f1 = [scores(item.git_log, item.oracle)["f1"] for item in scored]
    paired = paired_bootstrap(lineage_f1, git_f1)
    groups = {
        "all": list(scored),
        "rename_or_move": [item for item in scored if rename_or_move(item.function)],
        "nested_or_property": [
            item for item in scored if item.resolved.kind == "nested" or item.resolved.property_part
        ],
    }
    return {
        "schema": 1,
        "inputs": dict(inputs),
        "oracle": {
            "functions": len(scored),
            "labels": sum(len(item.function.labels) for item in scored),
            "projected_labels": sum(item.projected for item in scored),
            "commits": sum(len(item.oracle) for item in scored),
        },
        "functions": [_row(item) for item in scored],
        "means": {name: _means(items) for name, items in groups.items()},
        "claim_3": {
            "difference": round(paired.delta, 4),
            "interval": [round(paired.lo, 4), round(paired.hi, 4)],
            "n": paired.n,
            "prob_not_better": round(paired.prob_not_better, 4),
            "threshold": IDENTITY_MIN_F1_GAIN,
            "verdict": identity(paired.delta, paired.lo),
        },
    }


def render_json(found: Mapping[str, Any]) -> str:
    """Sorted keys and no timestamps: the same run always renders the same bytes."""
    return json.dumps(found, indent=2, sort_keys=True) + "\n"


def render_summary(found: Mapping[str, Any], out: Path) -> str:
    oracle, means, claim = found["oracle"], found["means"], found["claim_3"]
    lo, hi = claim["interval"]
    moved = means["rename_or_move"]
    return "\n".join(
        [
            f"oracle: {oracle['functions']} functions, {oracle['labels']} labels "
            f"({oracle['projected_labels']} moved to their merge), {oracle['commits']} commits",
            f"mean F1: lineage {means['all']['lineage']['f1']}, git log -L "
            f"{means['all']['git_log_l']['f1']}; the {moved['n']} rename-or-move functions: "
            f"{moved['lineage']['f1']} against {moved['git_log_l']['f1']}",
            f"claim 3: difference {claim['difference']} [{lo}, {hi}], n = {claim['n']}: "
            f"{claim['verdict']}",
            f"wrote {out}",
        ]
    )


def repository_state(root: Path) -> tuple[str, str]:
    """This repository's commit and the pre-registration's, if no tracked file is modified."""
    if _out(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise OracleRefused(
            "tracked files are modified: commit them first, so the recorded commit is the code "
            "that runs"
        )
    preregistration = _out(root, "log", "-1", "--format=%H", "--", PREREGISTRATION).strip()
    if not preregistration:
        raise OracleRefused(f"{PREREGISTRATION} is not committed")
    return _out(root, "rev-parse", "HEAD").strip(), preregistration


def oracle_files(codeshovel: Path, pinned: str, expected: int) -> tuple[list[Path], str]:
    """The oracle files as CodeShovel's pinned commit holds them, and a SHA-256 over them."""
    head = _out(codeshovel, "rev-parse", "HEAD").strip()
    if head != pinned:
        raise OracleRefused(f"CodeShovel is at {head[:10]}, not the pinned {pinned[:10]}")
    if _out(codeshovel, "status", "--porcelain", "--", str(ORACLE_DIR)).strip():
        raise OracleRefused("the oracle files differ from CodeShovel's commit")
    files = sorted((codeshovel / ORACLE_DIR).glob("*.json"))
    if len(files) != expected:
        raise OracleRefused(f"{len(files)} oracle files, not {expected}")
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.name.encode("utf-8") + b"\0" + file.read_bytes() + b"\0")
    return files, digest.hexdigest()


def check_labels(function: OracleFunction, clones: Path) -> None:
    """The start commit and every label are in the clone, and each label is an ancestor."""
    repo = clones / f"{function.project}.git"
    if not repo.is_dir():
        raise OracleRefused(f"{function.oracle}: no clone at {repo}")
    for commit in (function.start, *sorted(function.labels)):
        if _git(repo, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            raise OracleRefused(f"{function.oracle}: {commit} is not in {repo.name}")
    for label in sorted(function.labels):
        if _git(repo, "merge-base", "--is-ancestor", label, function.start).returncode != 0:
            raise OracleRefused(f"{function.oracle}: {label} is not an ancestor of its start")


def run_oracle(
    *,
    codeshovel: Path,
    clones: Path,
    out: Path,
    root: Path = REPOSITORY_ROOT,
    pinned: str = CODESHOVEL_COMMIT,
    expected_files: int = ORACLE_FILES,
) -> int:
    """`pace eval oracle`: claim 3, written to `out` only when every check passes."""
    try:
        commit, preregistration = repository_state(root)
        files, digest = oracle_files(codeshovel, pinned, expected_files)
        functions = read_oracle(files)
        for function in functions:
            check_labels(function, clones)
        scored = evaluate(functions, clones, progress=True)
        version = git_version()
        found = report(
            scored,
            {
                "repository_commit": commit,
                "preregistration_commit": preregistration,
                "codeshovel_commit": pinned,
                "oracle_files": len(files),
                "oracle_sha256": digest,
                "git": version,
                "git_log_l": log_algo(version),
                "detector": DETECTOR,
            },
        )
    except (RuntimeError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        print(f"nothing written: {exc}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_json(found), encoding="utf-8")
    print(render_summary(found, out))
    return 0
