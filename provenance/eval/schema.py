"""Golden-set record schema.

What this module is for: defining, once, what a scored question looks like, so
that the golden set outlives every implementation choice made after week 1.

The failure it prevents: a golden set that dies on the first chunker change.
Gold evidence here is keyed on identifiers that git and GitHub own -- commit sha,
PR number, issue number, review-comment id -- plus *symbol anchors*
``(path, qualified_name)``. It is NEVER keyed on ``chunk_id``.

Why never chunk_id
------------------
``chunk_id = sha256(scheme, repo, path, qualified_name, normalize_content(text))``
(see ``provenance/graph/ids.py``). It is a content hash. Re-splitting a function,
changing the overlap window, or stripping a docstring changes the content and
therefore changes the id, even though the evidence a human labelled is the same
piece of code with the same history. If gold rows pointed at chunk ids, every
chunker experiment would silently zero out recall and the only way to run the
experiment would be to re-label the set by hand. Anchors are re-resolved to the
current chunk ids at scoring time by ``provenance.eval.anchors.resolve_anchors``.
The corollary: ``chunking_strategy_version`` / ``embedding_model_version`` stay
*columns*, never hash inputs.

Two-tier relevance
------------------
``required_evidence``  -- the answer is wrong without it. Headline recall@k uses
                          these and only these. Small, strict, defensible.
``supporting_evidence``-- useful corroboration. Enters nDCG at a lower gain, so a
                          system that surfaces the required commit *and* the PR
                          discussion outranks one that surfaces the commit alone,
                          without inflating the headline number.

Leakage fields
--------------
``LeakageProbe`` is persisted at GENERATION time, not measured later. That is the
entire point. A naively generated golden set scores grep recall@10 = 0.986 -- the
question literally quotes the answer's identifiers -- and no amount of clever
retrieval measurement on such a set means anything. Recording the probe next to
the record makes the retention filter auditable after the fact: anyone can re-run
``passes_leakage_filter`` over the committed JSONL and see which records were kept
and why. Measured post-hoc, the numbers would reflect whatever the index looked
like that day.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GOLDEN_SCHEMA_VERSION = "v1"

#: A retained record must score at or below this with plain substring search.
#: Above it, the question is answerable by ``grep`` and measures nothing.
RETAINED_GREP_R10_MAX = 0.35

#: Stable evidence key: ("commit", sha) | ("pr", "1234") | ("issue", "5678")
#: | ("review_comment", "9012") | ("symbol", "path::qualified_name").
EvidenceKey: TypeAlias = tuple[str, str]


class QueryClass(StrEnum):
    """Question taxonomy. Also the bootstrap stratification key -- resampling
    happens *within* class so a resample can never contain zero unanswerables."""

    PROVENANCE = "provenance"
    ARCHAEOLOGY = "archaeology"
    ALTERNATIVES = "alternatives"
    CROSS_CUTTING = "cross_cutting"
    UNANSWERABLE = "unanswerable"


class UnanswerableStrategy(StrEnum):
    """How an unanswerable record was constructed.

    Recorded per record because the refusal metric is only credible if the
    negatives are *diverse*: a set built entirely from s5 (asking about code that
    does not exist) is trivially refusable by a name lookup and tells you nothing
    about whether the system refuses when the rationale simply was not written
    down.
    """

    S1_EMPTY_BLAME = "s1_empty_blame"  # code exists, blame leads to a bulk/format commit
    S2_VALUE_LEVEL = "s2_value_level"  # "why this constant?" -- never justified in writing
    S3_HELD_OUT = "s3_held_out"  # rationale exists but its source is excluded from the index
    S4_TEMPORAL = "s4_temporal"  # rationale postdates the pinned corpus commit
    S5_NONEXISTENT = "s5_nonexistent"  # the symbol/behaviour asked about does not exist


class SymbolAnchor(BaseModel):
    """A code location that survives re-chunking: file path + qualified name.

    ``qualified_name`` is the dotted name the chunker assigns
    (``SchedulerJobRunner._find_zombies``), or ``None`` for a file-level anchor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Repo-relative POSIX path at the pinned commit.")
    qualified_name: str | None = Field(
        default=None, description="Dotted symbol name; None means the whole file."
    )

    @field_validator("path")
    @classmethod
    def _posix_relative(cls, v: str) -> str:
        v = v.replace("\\", "/").strip()
        if v.startswith("/") or ".." in v.split("/"):
            raise ValueError(f"path must be repo-relative and normalised: {v!r}")
        return v

    @property
    def key(self) -> EvidenceKey:
        return ("symbol", f"{self.path}::{self.qualified_name or ''}")


class GoldEvidence(BaseModel):
    """The evidence a human labelled for one tier of one question.

    Every field is a *stable external* identifier. There is deliberately no
    ``chunk_ids`` field, and adding one is forbidden (``extra="forbid"`` makes an
    attempt fail loudly at load time rather than silently at scoring time).
    """

    model_config = ConfigDict(extra="forbid")

    commit_shas: list[str] = Field(default_factory=list)
    pr_numbers: list[int] = Field(default_factory=list)
    issue_numbers: list[int] = Field(default_factory=list)
    review_comment_ids: list[int] = Field(default_factory=list)
    symbol_anchors: list[SymbolAnchor] = Field(default_factory=list)

    @field_validator("commit_shas")
    @classmethod
    def _hex_shas(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for sha in v:
            s = sha.strip().lower()
            # Abbreviated shas are allowed (curation reads them off `git log`);
            # anchors.py matches on prefix. Anything shorter than 7 is ambiguous.
            if not (7 <= len(s) <= 40) or any(c not in "0123456789abcdef" for c in s):
                raise ValueError(f"not a git sha: {sha!r}")
            out.append(s)
        return out

    @field_validator("pr_numbers", "issue_numbers", "review_comment_ids")
    @classmethod
    def _positive(cls, v: list[int]) -> list[int]:
        if any(n <= 0 for n in v):
            raise ValueError("GitHub identifiers are positive integers")
        return v

    def is_empty(self) -> bool:
        return not self.keys()

    def keys(self) -> list[EvidenceKey]:
        """Flatten to the stable key form the anchor resolver and the citation
        metric both consume. Order is deterministic so hashes over it are stable."""
        keys: list[EvidenceKey] = []
        keys += [("commit", s) for s in self.commit_shas]
        keys += [("pr", str(n)) for n in self.pr_numbers]
        keys += [("issue", str(n)) for n in self.issue_numbers]
        keys += [("review_comment", str(n)) for n in self.review_comment_ids]
        keys += [a.key for a in self.symbol_anchors]
        return keys

    def __len__(self) -> int:
        return len(self.keys())


class LeakageProbe(BaseModel):
    """Baseline scores measured against this record at generation time.

    ``gold_symbol_df`` is the corpus document frequency of the rarest identifier
    the question mentions. A very high df means the question names something so
    common (``self``, ``session``, ``dag``) that lexical retrieval cannot be
    blamed for missing it; a df of 1 usually means the question quotes a unique
    identifier and grep will win -- which is exactly the leak the filter removes.
    """

    model_config = ConfigDict(extra="forbid")

    baseline_grep_r10: float = Field(ge=0.0, le=1.0)
    baseline_bm25_r10: float = Field(ge=0.0, le=1.0)
    gold_symbol_df: int = Field(ge=0)
    probed_at: datetime | None = None
    probe_index_version: str | None = None
    probe_corpus_commit: str | None = None


class GoldenRecord(BaseModel):
    """One scored question."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    qid: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._:-]+$")
    query_class: QueryClass
    question: str = Field(min_length=8)

    required_evidence: GoldEvidence = Field(default_factory=GoldEvidence)
    supporting_evidence: GoldEvidence = Field(default_factory=GoldEvidence)

    #: Free-form but conventional: "handwritten_seed_w1", "llm_backtranslate_v2",
    #: "pr_review_mined_v1". Prefixed "handwritten" records skip the leakage
    #: requirement because there is no generator to have measured it.
    generation_strategy: str = Field(min_length=1)
    unanswerable_strategy: UnanswerableStrategy | None = None
    leakage: LeakageProbe | None = None

    corpus_commit: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> GoldenRecord:
        unanswerable = self.query_class is QueryClass.UNANSWERABLE
        if unanswerable:
            if self.unanswerable_strategy is None:
                raise ValueError(f"{self.qid}: unanswerable records need a construction strategy")
            if not self.required_evidence.is_empty():
                raise ValueError(f"{self.qid}: unanswerable records must have no required evidence")
        else:
            if self.unanswerable_strategy is not None:
                raise ValueError(
                    f"{self.qid}: unanswerable_strategy set on a {self.query_class} record"
                )
            if self.required_evidence.is_empty():
                raise ValueError(
                    f"{self.qid}: answerable records need at least one required anchor"
                )
        if self.leakage is None and not self.is_handwritten:
            # Generated records without a probe cannot be retention-filtered, and an
            # unfiltered generated set is the 0.986-grep-recall failure mode.
            raise ValueError(
                f"{self.qid}: generated records must carry a leakage probe measured at "
                "generation time (generation_strategy="
                f"{self.generation_strategy!r})"
            )
        return self

    @property
    def is_handwritten(self) -> bool:
        return self.generation_strategy.startswith("handwritten")

    @property
    def expected_refusal(self) -> bool:
        return self.query_class is QueryClass.UNANSWERABLE

    @property
    def baseline_grep_r10(self) -> float | None:
        return None if self.leakage is None else self.leakage.baseline_grep_r10

    @property
    def baseline_bm25_r10(self) -> float | None:
        return None if self.leakage is None else self.leakage.baseline_bm25_r10

    @property
    def gold_symbol_df(self) -> int | None:
        return None if self.leakage is None else self.leakage.gold_symbol_df

    def all_evidence_keys(self) -> list[EvidenceKey]:
        return self.required_evidence.keys() + self.supporting_evidence.keys()


def passes_leakage_filter(
    record: GoldenRecord, *, max_grep_r10: float = RETAINED_GREP_R10_MAX
) -> bool:
    """Retention rule for generated records.

    Unanswerables and hand-written seeds are always retained; there is nothing for
    grep to leak in an unanswerable, and hand-written seeds are individually
    inspected. Generated answerables are retained only when plain substring search
    cannot already find the gold evidence.
    """
    if record.expected_refusal or record.is_handwritten:
        return True
    if record.leakage is None:  # unreachable via the validator, kept as a hard floor
        return False
    return record.leakage.baseline_grep_r10 <= max_grep_r10


def load_golden_set(path: Path | str) -> list[GoldenRecord]:
    """Read a ``.jsonl`` golden set.

    Blank lines and ``#`` comment lines are skipped so the file can carry a
    provenance header. A missing file yields an empty set -- week 1 has no golden
    records and the gate must still run green.
    """
    p = Path(path)
    if not p.exists():
        return []
    records: list[GoldenRecord] = []
    seen: set[str] = set()
    with p.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload: dict[str, Any] = json.loads(line)
                record = GoldenRecord.model_validate(payload)
            except Exception as exc:  # noqa: BLE001 -- want the line number in the message
                raise ValueError(f"{p}:{lineno}: {exc}") from exc
            if record.qid in seen:
                raise ValueError(f"{p}:{lineno}: duplicate qid {record.qid!r}")
            seen.add(record.qid)
            records.append(record)
    return records


def iter_golden_dir(directory: Path | str) -> Iterator[GoldenRecord]:
    """Load every ``*.jsonl`` under a directory, sorted for determinism."""
    for path in sorted(Path(directory).glob("*.jsonl")):
        yield from load_golden_set(path)


def write_golden_set(
    records: Iterable[GoldenRecord], path: Path | str, *, header: str = ""
) -> Path:
    """Write records as JSONL with a stable key order (so diffs are reviewable)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as fh:
        for line in header.splitlines():
            fh.write(f"# {line}\n" if not line.startswith("#") else f"{line}\n")
        for record in records:
            fh.write(json.dumps(record.model_dump(mode="json", exclude_none=True), sort_keys=True))
            fh.write("\n")
    return p
