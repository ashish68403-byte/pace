"""Checkpoint D's unanswerable questions: S3 held out, S4 after the pin, S5 nonexistent (slice D1).

docs/tasks/agent-refusal/001_unanswerable_questions.md and ADR-0006 §2. Each negative starts from
a retained question, chosen by salted hash, and keeps its split; its template and month follow
the source where its kind allows, so only the missing evidence tells it from an answerable one:

* S3 keeps the question, and its evidence becomes `held_out`, which retrieval must hide (D2);
* S4 asks about the source's function in a month after the corpus's pin;
* S5 asks about a name spliced from two functions alive in the source's file, one that lineage
  has never seen and no rationale document mentions.

A function is asked about after the pin once in the file, and an S5 name is used once, so dev
and test share no negative.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from provenance.eval.schema import (
    GoldenRecord,
    GoldEvidence,
    QueryClass,
    SymbolAnchor,
    UnanswerableStrategy,
)

SALT: Final = "pace-unanswerable-v1:"
GENERATION_STRATEGY: Final = "unanswerable_v0"
#: Per split, how many questions of each kind: one retained question in fourteen.
COUNTS: Final = {"dev": 300, "test": 214}
STRATEGIES: Final = {
    "s3": UnanswerableStrategy.S3_HELD_OUT,
    "s4": UnanswerableStrategy.S4_TEMPORAL,
    "s5": UnanswerableStrategy.S5_NONEXISTENT,
}
#: The corpus is pinned at 1e2ad803f7, committed on 10 Sep 2026.
AFTER_THE_PIN: Final = ("October 2026", "November 2026", "December 2026")
#: S5's tally, in the census's words.
S5_TALLY: Final = (
    "read",
    "no_parts",
    "tried",
    "seen_by_lineage",
    "already_used",
    "found_in_the_pool",
)
_CHANGED: Final = re.compile(r"^Why did `[^`]+` change in (?P<month>[A-Z][a-z]+ \d{4})\?$")
_ADDED: Final = re.compile(r"^Why was `[^`]+` added to `[^`]+`\?$")


class UnanswerableRefused(RuntimeError):
    """The negatives cannot be built as ADR-0006 §2 says. Nothing was written."""


def rank(key: str) -> str:
    """The salted hash every choice is ordered by."""
    return hashlib.sha256(f"{SALT}{key}".encode()).hexdigest()


def ranked(records: Sequence[GoldenRecord], kind: str) -> list[GoldenRecord]:
    """`records` in `kind`'s hash order, whatever order they came in."""
    return sorted(records, key=lambda record: rank(f"{kind}:{record.qid}"))


def function_of(record: GoldenRecord) -> tuple[str, str]:
    """A question's function, its one named supporting anchor: (path, qualified name)."""
    anchors = [a for a in record.supporting_evidence.symbol_anchors if a.qualified_name]
    if len(anchors) != 1:
        raise UnanswerableRefused(f"{record.qid}: {len(anchors)} functions, not one")
    return anchors[0].path, str(anchors[0].qualified_name)


def month_of(record: GoldenRecord) -> str | None:
    """A "changed" question's month, or None for an "added" one; other wording is refused."""
    changed = _CHANGED.match(record.question)
    if changed:
        return str(changed["month"])
    if _ADDED.match(record.question):
        return None
    raise UnanswerableRefused(f"{record.qid}: not one of ADR-0003's two templates")


def template_of(record: GoldenRecord) -> str:
    return "added" if month_of(record) is None else "changed"


def _negative(
    record: GoldenRecord,
    kind: str,
    question: str,
    anchor: SymbolAnchor,
    held_out: GoldEvidence | None = None,
) -> GoldenRecord:
    return GoldenRecord(
        qid=f"{kind}-{record.qid}",
        query_class=QueryClass.UNANSWERABLE,
        split=record.split,
        question=question,
        supporting_evidence=GoldEvidence(symbol_anchors=[anchor]),
        held_out=held_out,
        generation_strategy=GENERATION_STRATEGY,
        unanswerable_strategy=STRATEGIES[kind],
        corpus_commit=record.corpus_commit,
    )


def s3(record: GoldenRecord) -> GoldenRecord:
    """The question unchanged; its required evidence becomes what retrieval must hide."""
    path, name = function_of(record)
    anchor = SymbolAnchor(path=path, qualified_name=name)
    return _negative(record, "s3", record.question, anchor, held_out=record.required_evidence)


def s4(record: GoldenRecord) -> GoldenRecord:
    """Its function, asked about in a month after the corpus's pin."""
    path, name = function_of(record)
    month = AFTER_THE_PIN[int(rank(f"s4:{record.qid}"), 16) % len(AFTER_THE_PIN)]
    question = f"Why did `{name}` change in {month}?"
    return _negative(record, "s4", question, SymbolAnchor(path=path, qualified_name=name))


def _parts(name: str) -> list[str]:
    return [part for part in name.lstrip("_").split("_") if part]


def splice(source: str, partner: str) -> str | None:
    """`source`'s first parts, then `partner`'s last, split at underscores (ADR-0006 §2).

    The first half of the source's parts rounded up, the second half of the partner's rounded
    down but at least one, and the source's leading underscores kept: `_get_dag_runs` and
    `update_state` give `_get_dag_state`. None when either name is underscores alone.
    """
    mine, theirs = _parts(source), _parts(partner)
    if not mine or not theirs:
        return None
    lead = source[: len(source) - len(source.lstrip("_"))]
    first = mine[: (len(mine) + 1) // 2]
    second = theirs[len(theirs) - max(1, len(theirs) // 2) :]
    return lead + "_".join(first + second)


def spliced_name(record: GoldenRecord, partner: str) -> str | None:
    """The source's qualified name with its last part spliced: a method keeps its class."""
    _, name = function_of(record)
    owner, _, short = name.rpartition(".")
    new = splice(short, partner.rpartition(".")[2])
    if new is None:
        return None
    return f"{owner}.{new}" if owner else new


def s5(record: GoldenRecord, name: str) -> GoldenRecord:
    """Its file, template and month, asked about a function that exists nowhere."""
    path, _ = function_of(record)
    month = month_of(record)
    question = (
        f"Why did `{name}` change in {month}?" if month else f"Why was `{name}` added to `{path}`?"
    )
    return _negative(record, "s5", question, SymbolAnchor(path=path))


def choose_s3(
    records: Sequence[GoldenRecord], need: int
) -> tuple[list[GoldenRecord], Counter[str]]:
    """The first `need` questions by hash, held back from D3's answerable sample."""
    chosen = [s3(record) for record in ranked(records, "s3")[:need]]
    return chosen, Counter({"read": len(chosen)})


def choose_s4(
    records: Sequence[GoldenRecord], need: int, *, used: set[tuple[str, str]]
) -> tuple[list[GoldenRecord], Counter[str]]:
    """One question per function in hash order, until `need`.

    `used` holds the functions earlier splits asked about, and gains this split's.
    """
    chosen: list[GoldenRecord] = []
    tally: Counter[str] = Counter({"read": 0})
    for record in ranked(records, "s4"):
        if len(chosen) == need:
            break
        tally["read"] += 1
        function = function_of(record)
        if function not in used:
            used.add(function)
            chosen.append(s4(record))
    return chosen, tally


def _partners(record: GoldenRecord, partners: Mapping[str, Sequence[str]]) -> list[str]:
    """The other functions alive in the source's file, in this question's hash order."""
    path, name = function_of(record)
    others = [partner for partner in partners.get(path, ()) if partner != name]
    return sorted(others, key=lambda partner: rank(f"s5:{record.qid}:{partner}"))


def choose_s5(
    records: Sequence[GoldenRecord],
    need: int,
    *,
    partners: Mapping[str, Sequence[str]],
    seen: set[str],
    used: set[str],
    in_pool: Callable[[str], bool],
) -> tuple[list[GoldenRecord], Counter[str]]:
    """One spliced name per question in hash order, until `need` names pass every check.

    A name is rejected when a name lineage has seen ends in its last part (so neither it nor a
    namesake in another class exists), when an earlier S5 question uses it (`used`, which gains
    this split's names), or when a rationale document contains its last part (`in_pool`).
    """
    seen_short = {name.rpartition(".")[2] for name in seen}
    tally: Counter[str] = Counter(dict.fromkeys(S5_TALLY, 0))
    chosen: list[GoldenRecord] = []
    for record in ranked(records, "s5"):
        if len(chosen) == need:
            break
        tally["read"] += 1
        for partner in _partners(record, partners):
            name = spliced_name(record, partner)
            if name is None:
                tally["no_parts"] += 1
                continue
            tally["tried"] += 1
            short = name.rpartition(".")[2]
            if short in seen_short:
                tally["seen_by_lineage"] += 1
            elif name in used:
                tally["already_used"] += 1
            elif in_pool(short):
                tally["found_in_the_pool"] += 1
            else:
                used.add(name)
                chosen.append(s5(record, name))
                break
    return chosen, tally
