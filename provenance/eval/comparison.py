"""Claims 1 and 2: lineage against blame on the retained dev questions, scored once (slice C4).

docs/tasks/function-identity/004_comparison.md; ADR-0005 §1–§5 and §9; PREREGISTRATION.md
claims 1 and 2. Each question's function gets the arms slices C2 and C3a stored, built as their
censuses counted them and cross-checked against the censuses' committed summaries:

* A, the distinct filtered blame owners of its lines; blame unfiltered, the distinct plain
  owners (labelled, not pre-registered);
* L, its stored `git log -L` commits, and C, the commits of its lineage's versions, both
  without mechanical commits;
* B, the exact chance that a random subset of C as large as A holds the gold.

Recall per arm; paired differences stratified by query class, with D, the MDE and the
difference divided by the gold's validity; claims 1 and 2 in the pre-registered words, the
split of C - A, and the agreement check's qualification beside them.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from provenance.analysis.blame_census import _summary as census_summary
from provenance.eval.ablation import read_validity
from provenance.eval.generator import pool_version
from provenance.eval.metrics import (
    CI,
    PairedCI,
    bootstrap_ci,
    chain_hit,
    discordant,
    equal_size_chance,
    mde,
    paired_bootstrap,
)
from provenance.eval.oracle import repository_state
from provenance.eval.preregistration import COVERAGE_MIN_GAIN, coverage, selection
from provenance.eval.schema import load_golden_set, passes_leakage_filter
from provenance.graph.tables import (
    BLAME_RANGES,
    COMMITS,
    LINE_LOG_COMMITS,
    SYMBOL_LINEAGE,
    SYMBOL_VERSIONS,
)
from provenance.ingest.blame import Target, file_ids, golden_target, show
from provenance.lineage.build import DETECTOR
from provenance.parse.symbols import symbol_spans

#: Each question's values in the report: the four arms, then blame unfiltered.
ARMS: Final = ("A", "B", "C", "L", "blame_unfiltered")
#: The arms whose per-question value is 0 or 1: D and the MDE apply between two of them.
BINARY: Final = frozenset({"A", "C", "L", "blame_unfiltered"})
#: (system, baseline): C - A is claim 1, B - A claim 2, C - B the share due to volume,
#: L - A is reported, and blame unfiltered - A is labelled, not pre-registered.
DIFFERENCES: Final = (("C", "A"), ("B", "A"), ("C", "B"), ("L", "A"), ("blame_unfiltered", "A"))

_COMMITS: Final = f"SELECT commit_sha, is_mechanical FROM {COMMITS} WHERE repo_id = %s"
_DETECTORS: Final = f"SELECT DISTINCT detector_version FROM {SYMBOL_LINEAGE} WHERE repo_id = %s"
_ALIVE: Final = (
    f"SELECT lineage_id, current_path, current_qualified_name FROM {SYMBOL_LINEAGE} "
    "WHERE repo_id = %s AND death_commit IS NULL"
)
_VERSIONS: Final = (
    f"SELECT v.lineage_id, v.commit_sha FROM {SYMBOL_VERSIONS} v "
    f"JOIN {SYMBOL_LINEAGE} l USING (lineage_id) WHERE l.repo_id = %s"
)
_RANGES: Final = (
    "SELECT lower(line_range), upper(line_range), orig_commit_sha, effective_commit_sha "
    f"FROM {BLAME_RANGES} WHERE file_id = %s AND snapshot_sha = %s"
)
_LOGS: Final = (
    f"SELECT qualified_name, commit_sha FROM {LINE_LOG_COMMITS} "
    "WHERE repo_id = %s AND file_id = %s AND snapshot_sha = %s"
)


class ComparisonRefused(RuntimeError):
    """The comparison will not be scored; nothing was written."""


@dataclass(frozen=True)
class Arms:
    """One function's commit sets, mechanical commits removed where ADR-0005 §2 says."""

    blame: frozenset[str]
    unfiltered: frozenset[str]
    line_log: frozenset[str]
    lineage: frozenset[str]


def question_values(arms: Arms, gold: str) -> dict[str, float]:
    """One question's value in each arm (ADR-0005 §2–§3): a hit, or B's exact chance."""
    golds = (gold,)
    return {
        "A": chain_hit(arms.blame, golds),
        "B": equal_size_chance(len(arms.blame), arms.lineage, golds),
        "C": chain_hit(arms.lineage, golds),
        "L": chain_hit(arms.line_log, golds),
        "blame_unfiltered": chain_hit(arms.unfiltered, golds),
    }


@dataclass(frozen=True)
class Question:
    """One retained dev record: its function, its one gold commit and its query class."""

    qid: str
    query_class: str
    function: tuple[str, str]
    gold: str


def read_questions(golden: Path) -> list[Question]:
    """The retained dev records as questions, each with one function and one full gold commit."""
    questions: list[Question] = []
    for record in load_golden_set(golden):
        if record.split != "dev" or not passes_leakage_filter(record):
            continue
        anchors = (
            *record.required_evidence.symbol_anchors,
            *record.supporting_evidence.symbol_anchors,
        )
        functions = {(a.path, str(a.qualified_name)) for a in anchors if a.qualified_name}
        golds = record.required_evidence.commit_shas
        if len(functions) != 1 or len(golds) != 1 or len(golds[0]) != 40:
            raise ComparisonRefused(f"{record.qid}: not one function and one full gold commit")
        (function,) = functions
        questions.append(Question(record.qid, str(record.query_class.value), function, golds[0]))
    return sorted(questions, key=lambda question: question.qid)


def load_arms(
    conn: Any, *, repo: Path, repo_id: int, target: Target
) -> dict[tuple[str, str], Arms]:
    """Each target function's arms from the stored rows, as C2's and C3a's censuses count them."""
    detectors = {str(row[0]) for row in conn.execute(_DETECTORS, (repo_id,))}
    if detectors != {DETECTOR}:
        raise ComparisonRefused(
            f"symbol_lineage holds {sorted(detectors)}, not {DETECTOR}: run `pace lineage build`"
        )
    mechanical = {str(sha) for sha, flag in conn.execute(_COMMITS, (repo_id,)) if flag}
    alive = {
        (str(path), str(name)): int(lineage_id)
        for lineage_id, path, name in conn.execute(_ALIVE, (repo_id,))
    }
    versions: dict[int, set[str]] = defaultdict(set)
    for lineage_id, sha in conn.execute(_VERSIONS, (repo_id,)):
        versions[int(lineage_id)].add(str(sha))
    by_path: dict[str, list[str]] = defaultdict(list)
    for path, name in target.functions:
        by_path[path].append(name)
    ids = file_ids(conn, repo_id, sorted(by_path))
    arms: dict[tuple[str, str], Arms] = {}
    for path in sorted(by_path):
        spans = symbol_spans(show(repo, target.pinned, path), path) or {}
        owner: dict[int, tuple[str, str | None]] = {}
        for low, high, orig, effective in conn.execute(_RANGES, (ids[path], target.pinned)):
            for line in range(int(low), int(high)):
                owner[line] = (str(orig), None if effective is None else str(effective))
        logged: dict[str, set[str]] = defaultdict(set)
        for name, sha in conn.execute(_LOGS, (repo_id, ids[path], target.pinned)):
            logged[str(name)].add(str(sha))
        for name in sorted(by_path[path]):
            if name not in spans:
                raise ComparisonRefused(f"{path}: {name!r} is not a function at the pin")
            lines = {line for first, last in spans[name] for line in range(first, last + 1)}
            if not lines <= owner.keys() or not logged.get(name):
                raise ComparisonRefused(f"{path}::{name} has no stored blame or git log -L")
            if (path, name) not in alive:
                raise ComparisonRefused(f"{path}::{name} has no identity alive at the pin")
            owners = [owner[line] for line in sorted(lines)]
            arms[(path, name)] = Arms(
                blame=frozenset(kept for _, kept in owners if kept is not None),
                unfiltered=frozenset(orig for orig, _ in owners),
                line_log=frozenset(logged[name] - mechanical),
                lineage=frozenset(versions[alive[(path, name)]] - mechanical),
            )
    return arms


def question_row(question: Question, arms: Arms) -> dict[str, Any]:
    """One line of the per-question file."""
    path, name = question.function
    return {
        "qid": question.qid,
        "query_class": question.query_class,
        "path": path,
        "function": name,
        "gold": question.gold,
        "size_a": len(arms.blame),
        "size_c": len(arms.lineage),
        "size_l": len(arms.line_log),
        **question_values(arms, question.gold),
    }


def qualification(lineage: Mapping[str, Any]) -> dict[str, Any]:
    """The agreement check's result, which the pre-registration says qualifies claims 1 to 3."""
    agreement = lineage["agreement"]
    return {
        "source": "results/lineage.json",
        "agreement_check": {
            "mean_f1": agreement["mean_f1"],
            "threshold": agreement["threshold"],
            "verdict": agreement["verdict"],
        },
    }


def summaries(arms: Mapping[tuple[str, str], Arms]) -> dict[str, dict[str, float | int]]:
    """Per-function arm sizes, summarised exactly as the censuses summarise them."""
    ordered = [arms[function] for function in sorted(arms)]
    return {
        "arm_a": census_summary([len(item.blame) for item in ordered]),
        "blame_unfiltered": census_summary([len(item.unfiltered) for item in ordered]),
        "arm_l": census_summary([len(item.line_log) for item in ordered]),
        "arm_c": census_summary([len(item.lineage) for item in ordered]),
    }


def cross_check(
    found: Mapping[str, Any],
    blame_arms: Mapping[str, Any],
    lineage: Mapping[str, Any],
    target: Target,
) -> dict[str, Any]:
    """The arms to be scored are the arms the censuses reported, or nothing is written."""
    expected_inputs = (target.source_sha256, target.pinned, len(target.functions))
    for name, census in (("blame_arms.json", blame_arms), ("lineage.json", lineage)):
        inputs = census["inputs"]
        if (inputs["golden_sha256"], inputs["pinned"], inputs["functions"]) != expected_inputs:
            raise ComparisonRefused(
                f"{name} was made for another golden set, pin or function count"
            )
    expected = {**blame_arms["commits_per_function"], "arm_c": lineage["arm_c_per_function"]}
    if dict(found) != expected:
        raise ComparisonRefused(
            "the arms' sizes differ from the censuses': re-run `pace measure blame` and "
            "`pace measure lineage` on the stored rows first"
        )
    return {"commits_per_function": dict(found), "agrees": True}


def _ci(found: CI) -> dict[str, float | int]:
    return {
        "point": round(found.point, 4),
        "lo": round(found.lo, 4),
        "hi": round(found.hi, 4),
        "n": found.n,
    }


def _recall(rows: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    values = [float(row[arm]) for row in rows]
    classes = [str(row["query_class"]) for row in rows]
    by_class = {
        name: _ci(bootstrap_ci([v for v, c in zip(values, classes, strict=True) if c == name]))
        for name in sorted(set(classes))
    }
    return {"all": _ci(bootstrap_ci(values, classes)), "by_class": by_class}


def _difference(
    rows: Sequence[Mapping[str, Any]], system: str, baseline: str, validity: Mapping[str, Any]
) -> tuple[dict[str, Any], PairedCI]:
    ours = [float(row[system]) for row in rows]
    theirs = [float(row[baseline]) for row in rows]
    paired = paired_bootstrap(ours, theirs, [str(row["query_class"]) for row in rows])
    found: dict[str, Any] = {
        "difference": round(paired.delta, 4),
        "interval": [round(paired.lo, 4), round(paired.hi, 4)],
        "n": paired.n,
        "prob_not_better": round(paired.prob_not_better, 4),
    }
    if system in BINARY and baseline in BINARY:
        d, n = discordant(ours, theirs)
        found["discordant"] = d
        found["mde"] = None if d == 0 else round(mde(n, d / n), 4)
    rate = float(validity["rate"])
    low, high = (float(x) for x in validity["ci95"])
    # ADR-0004 §8: the raw difference is the claim; divided, it shows what empty gold may hide.
    found["over_validity"] = {
        "at_point": round(paired.delta / rate, 4),
        "at_upper": round(paired.delta / high, 4),
        "at_lower": round(paired.delta / low, 4),
    }
    return found, paired


def report(
    rows: Sequence[Mapping[str, Any]],
    *,
    inputs: Mapping[str, Any],
    cross_checked: Mapping[str, Any],
    validity: Mapping[str, Any],
    qualification: Mapping[str, Any],
) -> dict[str, Any]:
    """The table, the decisions and the qualification, from the per-question rows."""
    differences: dict[str, Any] = {}
    paired: dict[str, PairedCI] = {}
    for system, baseline in DIFFERENCES:
        name = f"{system}-{baseline}"
        differences[name], paired[name] = _difference(rows, system, baseline, validity)
    c_a, b_a, c_b = paired["C-A"], paired["B-A"], paired["C-B"]
    return {
        "schema": 1,
        "inputs": dict(inputs),
        "cross_check": dict(cross_checked),
        "validity": dict(validity),
        "recall": {arm: _recall(rows, arm) for arm in ARMS},
        "arm_sizes": {
            "A": census_summary([int(row["size_a"]) for row in rows]),
            "C": census_summary([int(row["size_c"]) for row in rows]),
            "L": census_summary([int(row["size_l"]) for row in rows]),
        },
        "differences": differences,
        "claim_1": {
            "difference": round(c_a.delta, 4),
            "interval": [round(c_a.lo, 4), round(c_a.hi, 4)],
            "threshold": COVERAGE_MIN_GAIN,
            "verdict": coverage(c_a.delta, c_a.lo),
        },
        "claim_2": {
            "difference": round(b_a.delta, 4),
            "interval": [round(b_a.lo, 4), round(b_a.hi, 4)],
            "reading": selection(b_a.lo, b_a.hi),
        },
        "split": {
            "C-A": round(c_a.delta, 4),
            "C-B": round(c_b.delta, 4),
            "B-A": round(b_a.delta, 4),
            "adds_up": abs(c_a.delta - (c_b.delta + b_a.delta)) < 1e-9,
        },
        "qualification": dict(qualification),
    }


def render_json(found: Mapping[str, Any]) -> str:
    """Sorted keys and no timestamps: the same rows always render the same bytes."""
    return json.dumps(found, indent=2, sort_keys=True) + "\n"


def per_question_jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    """One line per question, in qid order: every number in the report comes from these."""
    return "".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in rows)


def compare(
    conn: Any,
    *,
    repo: Path,
    repo_id: int,
    golden: Path,
    blame_arms: Path,
    lineage_results: Path,
    audit: Path,
    root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The report and the per-question rows, or a refusal before anything is written."""
    commit, preregistration = repository_state(root)
    target = golden_target(golden)
    questions = read_questions(golden)
    arms = load_arms(conn, repo=repo, repo_id=repo_id, target=target)
    blame_census = json.loads(blame_arms.read_text(encoding="utf-8"))
    lineage_census = json.loads(lineage_results.read_text(encoding="utf-8"))
    checked = cross_check(summaries(arms), blame_census, lineage_census, target)
    version = pool_version(conn, repo_id)
    validity = read_validity(audit, version)
    if validity["status"] != "current":
        raise ComparisonRefused(f"the gold's validity is {validity['status']} ({audit})")
    rows = [question_row(question, arms[question.function]) for question in questions]
    inputs = {
        "repository_commit": commit,
        "preregistration_commit": preregistration,
        "golden_sha256": target.source_sha256,
        "pinned": target.pinned,
        "questions": len(questions),
        "functions": len(target.functions),
        "pool_version": version,
        "blame": blame_census["inputs"]["blame"],
        "line_log": blame_census["inputs"]["line_log"],
        "detector": DETECTOR,
    }
    found = report(
        rows,
        inputs=inputs,
        cross_checked=checked,
        validity=validity,
        qualification=qualification(lineage_census),
    )
    return found, rows
