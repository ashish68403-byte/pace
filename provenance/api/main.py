"""FastAPI app: POST /ask and GET /healthz. The Phase-0 exit criterion.

What this proves BEFORE any retrieval code exists: one HTTP request produces ONE
complete OpenTelemetry trace whose root is ``pace.query``, with a child span for
every pipeline stage, and the response body carries the trace id back to the
caller. Every stage is a stub that records ``pace.unimplemented = true``; the
shape of the trace is therefore already correct and each stage gets replaced in
place later, without the trace shape ever being "designed" retroactively.

Failure this prevents: discovering in week 10 that the agent loop, the reranker
and the ingest workers each invented their own instrumentation and no single
trace ever spans a whole request.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from fastapi import FastAPI
from opentelemetry import trace

from provenance.api.schemas import (
    AskRequest,
    AskResponse,
    Budget,
    DegradedSubsystem,
    ExtensionVersion,
    HealthResponse,
    Trajectory,
)
from provenance.config import settings
from provenance.obs.otel import code_version, current_trace_id, setup_tracing
from provenance.obs.spans import (
    ATTR_UNIMPLEMENTED,
    PIPELINE_STAGES,
    SPAN_QUERY,
    run_in_thread_with_context,
    stage,
    unimplemented_stage,
)

# Extensions this code was written against. Reported by /healthz so a version
# drift (pg_search retokenising differently, pgvector changing index defaults)
# is visible as a fact, not as mysteriously worse recall.
EXPECTED_EXTENSIONS = ("vector", "pg_search")


def index_version() -> str:
    """Identity of the index that served an answer.

    Two runs with different values are not comparable; the eval refuses to diff
    across them. Deliberately NOT part of any chunk_id -- see graph/ids.py.
    """
    return f"{settings.chunking_strategy_version}+{settings.embedding_model_version}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Install tracing before the first request is served."""
    setup_tracing(app)
    yield


app = FastAPI(
    title="PACE",
    summary="Provenance-Aware Code Intelligence",
    version=code_version(),
    lifespan=lifespan,
)
# Instrumentation is also applied here, not only in lifespan: instrument_app must
# run before the middleware stack is frozen by the first request, and setup_tracing
# is idempotent, so doing it at import time is the safe belt-and-braces order.
setup_tracing(app)


def _db_health() -> dict[str, Any]:
    """Synchronous database probe. Runs in a worker thread (psycopg pool is sync).

    An unreachable or degraded database is a DEGRADED healthcheck with the error
    text attached, not a 500 with a stack trace only in the logs. A bug in *this
    function* is a 500, on purpose -- see the except clause.

    Row shape: `connection()` hands out TUPLES (`graph.db.connection`,
    row_factory=tuple_row), so `row[0]` is the version string and
    `for name, ver in ...` unpacks each (extname, extversion) pair. Under the
    old dict_row default `row[0]` raised KeyError: 0 and the unpack yielded the
    two dict KEYS -- which is how /healthz reported extensions literally named
    "extname"/"extversion" and a perfectly healthy database as "degraded".
    If you ever pass row_factory=dict_row here, change both lines with it.
    """
    result: dict[str, Any] = {
        "ok": False,
        "error": None,
        "server_version": None,
        "extensions": [],
    }
    try:
        from provenance.graph.db import connection

        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT version()")
            row = cur.fetchone()
            result["server_version"] = row[0] if row else None
            cur.execute(
                "SELECT extname, extversion FROM pg_extension"
                " WHERE extname = ANY(%s) ORDER BY extname",
                (list(EXPECTED_EXTENSIONS),),
            )
            result["extensions"] = [{"name": name, "version": ver} for name, ver in cur.fetchall()]
            result["ok"] = True
    except (psycopg.Error, OSError) as exc:
        # NARROW, deliberately. psycopg.Error covers every server-side and
        # connection failure, and psycopg_pool's PoolTimeout / PoolClosed
        # subclass psycopg.OperationalError, so a database that is down, slow,
        # unmigrated or missing an extension still degrades gracefully. OSError
        # catches a socket or DNS failure that escapes psycopg.
        #
        # What must NOT be caught is a bug in the six lines above: a TypeError
        # or KeyError from a row-shape change, an ImportError from a broken
        # install, an AttributeError from a renamed helper. A blanket
        # `except Exception` turned exactly that class of defect into the string
        # "degraded" and hid it for the life of the scaffold. Let it 500.
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness plus database reachability and extension versions."""
    with stage("pace.healthz"):
        # run_in_thread_with_context, not to_thread: the executor drops contextvars
        # and the db span would otherwise start a fresh orphan trace.
        info = await run_in_thread_with_context(_db_health)

    degraded: list[DegradedSubsystem] = []
    if not info["ok"]:
        degraded.append(DegradedSubsystem.DATABASE)

    return HealthResponse(
        status="ok" if info["ok"] else "degraded",
        service=settings.service_name,
        version=code_version(),
        database_ok=bool(info["ok"]),
        database_error=info["error"],
        server_version=info["server_version"],
        extensions=[ExtensionVersion(**e) for e in info["extensions"]],
        degraded=degraded,
    )


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    """Answer a why-is-this-code-like-this question. Currently all stages stubbed.

    Emits the full ``pace.query`` trace: one root span, one child per pipeline
    stage, each marked unimplemented. The response reports every stubbed stage in
    ``degraded`` and refuses, because a system with no retrieval has no evidence
    -- and PACE refusing when it has no evidence is the correct behaviour, not a
    placeholder.
    """
    started = time.perf_counter()
    tracer = trace.get_tracer("provenance.api")

    # Root span. FastAPI auto-instrumentation already made a server span; this is
    # the semantic root the rest of the pipeline hangs off, and the id we return
    # is this trace's id (the same trace as the server span).
    with tracer.start_as_current_span(SPAN_QUERY) as root:
        root.set_attribute("pace.question_len", len(req.question))
        root.set_attribute("pace.top_k", req.top_k)
        root.set_attribute("pace.max_hops", req.max_hops)
        root.set_attribute("pace.index_version", index_version())
        if req.path:
            root.set_attribute("pace.path", req.path)
        if req.qualified_name:
            root.set_attribute("pace.qualified_name", req.qualified_name)

        degraded: list[DegradedSubsystem] = []
        for span_name in PIPELINE_STAGES:
            with unimplemented_stage(span_name) as span:
                span.set_attribute("pace.stub_reason", "phase-0 scaffold")
            subsystem = _STAGE_TO_SUBSYSTEM.get(span_name)
            if subsystem is not None and subsystem not in degraded:
                degraded.append(subsystem)

        trace_id = current_trace_id()
        root.set_attribute("pace.refused", True)
        root.set_attribute(ATTR_UNIMPLEMENTED, True)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return AskResponse(
            answer="",
            refused=True,
            refusal_reason=(
                "pipeline not implemented: no retrieval, no evidence, so no "
                "grounded answer can be produced"
            ),
            citations=[],
            trajectory=Trajectory(hops=0, tools_called=[], stopped_reason="no_evidence"),
            budget=Budget(wall_clock_ms=elapsed_ms),
            degraded=degraded,
            trace_id=trace_id,
            index_version=index_version(),
        )


# Which subsystem each stubbed stage degrades. Kept next to the endpoint because
# it is a property of this scaffold, not of the naming convention.
_STAGE_TO_SUBSYSTEM: dict[str, DegradedSubsystem] = {
    "pace.embed_query": DegradedSubsystem.EMBEDDER,
    "pace.search.lexical": DegradedSubsystem.LEXICAL_SEARCH,
    "pace.search.dense": DegradedSubsystem.DENSE_SEARCH,
    "pace.rerank": DegradedSubsystem.RERANKER,
    "pace.agent.step": DegradedSubsystem.AGENT,
    "pace.synthesize": DegradedSubsystem.SYNTHESIZER,
    "pace.verify": DegradedSubsystem.VERIFIER,
}
