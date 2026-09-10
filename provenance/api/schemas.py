"""Public request/response contract for PACE.

This response model is the ONE thing every other component agrees on: the CLI
prints it, the eval harness scores it, the demo UI renders it. It is frozen
early and deliberately, because changing it later means rewriting the golden-set
scorer.

Three decisions worth stating outright:

* ``refused`` is a first-class boolean, not an answer string starting with
  "I don't know". Refusal is a CORRECT outcome for PACE -- when the rationale was
  never written down, the honest answer is to say so -- and the eval must be able
  to score refusals separately from wrong answers. A string sniff cannot do that.
* ``citations`` never carry a chunk_id as their identity. They key on the durable
  upstream object (commit_sha, pr_number, issue_number, review_comment_id, or
  path + qualified_name) so a citation survives a re-chunk. Same invariant as the
  golden set.
* ``trace_id`` and ``index_version`` ride in the body, not only in headers. A bug
  report is usually a screenshot; the screenshot has to contain enough to find
  the trace and to know which index produced it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class SourceType(StrEnum):
    """Where a cited piece of evidence came from.

    Deliberately closed: PACE claims to cite git history and review discussion.
    Adding a source type is a scoring-affecting change and should require an
    edit here.
    """

    COMMIT = "commit"
    PR = "pr"
    ISSUE = "issue"
    REVIEW_COMMENT = "review_comment"
    CODE = "code"
    DOC = "doc"


class Span(BaseModel):
    """Byte or line range inside the cited source, for highlighting.

    Ranges are half-open [start, end). ``unit`` says which coordinate space:
    line numbers for code, character offsets for prose bodies.
    """

    model_config = ConfigDict(extra="forbid")

    unit: Literal["line", "char"] = "line"
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class Citation(BaseModel):
    """One piece of evidence supporting a sentence of the answer.

    ``id`` is the natural key of the source object -- a commit sha, a PR number,
    an issue number, a review comment id, or "path::qualified_name" for code --
    NOT a chunk_id. chunk_ids change whenever the chunker changes; citations and
    gold evidence must outlive that.
    """

    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    id: str = Field(min_length=1, description="Natural key of the source object.")
    span: Span | None = None
    quote: str = Field(
        default="",
        description="Verbatim text from the source. Must appear in the source; "
        "the verifier checks this rather than trusting the model.",
    )
    url: str | None = None
    score: float | None = Field(default=None, description="Post-fusion relevance.")


class ToolCall(BaseModel):
    """One tool invocation made by the agent, summarised for the trajectory."""

    model_config = ConfigDict(extra="forbid")

    name: str
    count: int = Field(default=1, ge=1)


class Trajectory(BaseModel):
    """What the agent actually did. The trace has the detail; this is the summary.

    Kept in the body so the eval can assert budget discipline (hop counts, tool
    mix) without needing a running trace backend.
    """

    model_config = ConfigDict(extra="forbid")

    hops: int = Field(default=0, ge=0, description="Agent loop iterations.")
    tools_called: list[ToolCall] = Field(default_factory=list)
    stopped_reason: str | None = Field(
        default=None,
        description="Why the loop ended: answered | budget_exhausted | no_evidence.",
    )


class Budget(BaseModel):
    """Resources one answer consumed. Reported on every response, including refusals.

    Cost is per-request and in USD; wall clock is measured around the root span so
    it matches the trace duration.
    """

    model_config = ConfigDict(extra="forbid")

    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0, description="tokens_in + tokens_out.")
    cost_usd: float = Field(default=0.0, ge=0.0)
    wall_clock_ms: float = Field(default=0.0, ge=0.0)


class DegradedSubsystem(StrEnum):
    """Subsystems that can be missing without failing the request.

    A degraded answer is still an answer, but the eval must be able to exclude it
    from headline metrics -- otherwise a broken reranker shows up as a model
    quality regression.
    """

    LEXICAL_SEARCH = "lexical_search"
    DENSE_SEARCH = "dense_search"
    RERANKER = "reranker"
    EMBEDDER = "embedder"
    AGENT = "agent"
    SYNTHESIZER = "synthesizer"
    VERIFIER = "verifier"
    DATABASE = "database"
    GITHUB_ENRICHMENT = "github_enrichment"


class AskRequest(BaseModel):
    """A question about why some code is the way it is."""

    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, Field(min_length=1, max_length=2000)]
    path: str | None = Field(
        default=None, description="Repo-relative file the question is about, if known."
    )
    qualified_name: str | None = Field(
        default=None, description="Dotted symbol name inside `path`, if known."
    )
    top_k: int = Field(default=10, ge=1, le=100)
    max_hops: int = Field(default=4, ge=0, le=16)
    allow_refusal: bool = Field(
        default=True,
        description="Set false ONLY in ablations. Refusal is a feature, not a bug.",
    )


class AskResponse(BaseModel):
    """The public answer contract. Every field is always present."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(default="", description="Empty string when refused is true.")
    refused: bool = False
    refusal_reason: str | None = Field(
        default=None,
        description="Required when refused; e.g. 'rationale never written down'.",
    )
    citations: list[Citation] = Field(default_factory=list)
    trajectory: Trajectory = Field(default_factory=Trajectory)
    budget: Budget = Field(default_factory=Budget)
    degraded: list[DegradedSubsystem] = Field(
        default_factory=list,
        description="Subsystems that were unavailable or stubbed for this answer.",
    )
    trace_id: str | None = Field(
        default=None, description="32-hex OpenTelemetry trace id for this request."
    )
    index_version: str = Field(
        default="",
        description="chunking_strategy_version + embedding_model_version of the "
        "index that served this answer. Two runs with different values are not "
        "comparable.",
    )


class ExtensionVersion(BaseModel):
    """Installed Postgres extension, as reported by pg_extension."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class HealthResponse(BaseModel):
    """Liveness plus the facts that actually break a deployment.

    The interesting failure is not "is the process up" but "is the database
    reachable and are pgvector / pg_search the versions this code was written
    against". Those go in the body.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"] = "ok"
    service: str
    version: str
    database_ok: bool = False
    database_error: str | None = None
    server_version: str | None = None
    extensions: list[ExtensionVersion] = Field(default_factory=list)
    degraded: list[DegradedSubsystem] = Field(default_factory=list)
