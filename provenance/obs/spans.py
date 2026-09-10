"""Span naming convention for the WHOLE pipeline, plus the async context helpers.

Two failures this module prevents.

1. Drifting span names. Every stage of PACE is written weeks apart; if the
   retrieval stage calls its span "search" and the eval harness looks for
   "pace.search.lexical", the trace-derived metrics silently read zero. The names
   live here as constants and NOWHERE else. Adding a stage means adding a
   constant here first.

2. Broken traces across async boundaries. FastAPI auto-instrumentation covers the
   request span and outbound httpx calls. It does NOT cover the three places a
   trace actually breaks in this codebase:
     (a) asyncio.create_task  -- copies context AT CREATION TIME
     (b) asyncio.to_thread / run_in_executor -- does NOT carry contextvars
     (c) queue workers -- separate PROCESSES, no shared context at all
   Helpers for (b) and (c) are implemented below.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

# --------------------------------------------------------------------------
# The convention. Root is pace.query; everything else is a descendant of it.
# --------------------------------------------------------------------------
SPAN_QUERY = "pace.query"  # root span for one /ask request
SPAN_PARSE_QUERY = "pace.parse_query"
SPAN_EMBED_QUERY = "pace.embed_query"
SPAN_SEARCH_LEXICAL = "pace.search.lexical"
SPAN_SEARCH_DENSE = "pace.search.dense"
SPAN_FUSE_RRF = "pace.fuse.rrf"
SPAN_RERANK = "pace.rerank"
SPAN_AGENT_STEP = "pace.agent.step"
SPAN_SYNTHESIZE = "pace.synthesize"
SPAN_VERIFY = "pace.verify"

_TOOL_SPAN_PREFIX = "pace.tool."

#: Ordered pipeline stages, used by the API to emit one stub child span each and
#: by the eval harness to assert a trace is complete. Order is execution order.
PIPELINE_STAGES: tuple[str, ...] = (
    SPAN_PARSE_QUERY,
    SPAN_EMBED_QUERY,
    SPAN_SEARCH_LEXICAL,
    SPAN_SEARCH_DENSE,
    SPAN_FUSE_RRF,
    SPAN_RERANK,
    SPAN_AGENT_STEP,
    SPAN_SYNTHESIZE,
    SPAN_VERIFY,
)

#: Attribute set on any span whose implementation does not exist yet. The eval
#: harness treats a trace containing this attribute as a scaffold run, never as a
#: real answer -- that is what stops a stubbed pipeline from quietly scoring.
ATTR_UNIMPLEMENTED = "pace.unimplemented"
ATTR_STAGE = "pace.stage"
ATTR_DEGRADED = "pace.degraded"

_tracer = trace.get_tracer("provenance.obs")


def tool_span_name(tool: str) -> str:
    """Span name for an agent tool invocation: ``pace.tool.<name>``."""
    return _TOOL_SPAN_PREFIX + tool


@contextmanager
def stage(name: str, **attrs: Any) -> Iterator[Span]:
    """Open a pipeline span named ``name`` with ``attrs`` set.

    Exceptions are recorded and the span status set to ERROR before re-raising,
    so a failed stage is visible in the trace rather than merely absent.
    """
    with _tracer.start_as_current_span(name, kind=SpanKind.INTERNAL) as span:
        span.set_attribute(ATTR_STAGE, name)
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:  # noqa: BLE001 - recorded then re-raised
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


@contextmanager
def unimplemented_stage(name: str, **attrs: Any) -> Iterator[Span]:
    """A stage span for a stage that does not exist yet.

    Emitting these from day one means the trace shape is fixed BEFORE the
    pipeline is written: each stage is later replaced in place, and any trace
    still carrying ``pace.unimplemented`` is unmistakably a scaffold run.
    """
    with stage(name, **attrs) as span:
        span.set_attribute(ATTR_UNIMPLEMENTED, True)
        yield span


# --------------------------------------------------------------------------
# (a) asyncio.create_task
# --------------------------------------------------------------------------
P = ParamSpec("P")
T = TypeVar("T")


def spawn_task(coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
    """Create a task, documenting the context-capture rule.

    GOTCHA (a): ``asyncio.create_task`` copies the CURRENT context at CREATION
    time, not at await time. That is usually what you want -- the child span
    parents to whatever span was active where the task was created. But it means
    creating tasks OUTSIDE the stage span and then awaiting them inside it
    silently parents every task to the wrong span (or to no span at all), and a
    task created inside a ``with stage(...)`` block that outlives the block ends
    up parented to a span that has already closed.

    There is nothing to fix here in code -- the copy is automatic. This wrapper
    exists so the rule has somewhere to live: create the task inside the
    ``stage()`` block it belongs to, and await it before that block exits.
    """
    return asyncio.create_task(coro, name=name)


# --------------------------------------------------------------------------
# (b) asyncio.to_thread / run_in_executor
# --------------------------------------------------------------------------
async def run_in_thread_with_context(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run ``func`` in a worker thread with the OTel context carried across.

    GOTCHA (b): ``asyncio.to_thread`` happens to copy contextvars, but
    ``loop.run_in_executor`` does NOT -- and neither does a raw
    ``ThreadPoolExecutor.submit``, which is what psycopg's sync pool, tree-sitter
    parsing and any local ONNX embedding call end up behind. A span started
    inside such a worker becomes a new ROOT span: an orphan trace with one span
    in it, which looks exactly like "the stage never ran".

    Fix: capture ``otel_context.get_current()`` on the caller side, then
    ``attach`` it INSIDE the worker before doing anything, and detach after.
    Always detach -- executor threads are reused, so a leaked attach poisons the
    next unrelated job that lands on that thread.
    """
    parent = otel_context.get_current()

    def _worker() -> T:
        token = otel_context.attach(parent)
        try:
            return func(*args, **kwargs)
        finally:
            otel_context.detach(token)

    loop = asyncio.get_running_loop()
    # run_in_executor(None, ...) uses the default executor and drops contextvars,
    # which is precisely why _worker re-attaches by hand.
    return await loop.run_in_executor(None, _worker)


def with_current_context(func: Callable[P, T]) -> Callable[P, T]:
    """Decorator form of the above, for callables handed to an executor directly.

    Binds the context present when the decorator itself RUNS, so apply it at the
    call site (``executor.submit(with_current_context(f), x)``), never as an
    ``@`` decorator at module import time -- at import there is no active span
    and you would bind an empty context forever.
    """
    parent = otel_context.get_current()

    @functools.wraps(func)
    def _wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        token = otel_context.attach(parent)
        try:
            return func(*args, **kwargs)
        finally:
            otel_context.detach(token)

    return _wrapped


def copy_context_for_thread() -> contextvars.Context:
    """Snapshot of the caller's contextvars, for ``Context.run`` in a thread.

    Alternative to :func:`with_current_context` when the callee is third-party
    code you cannot wrap: ``copy_context_for_thread().run(their_function, arg)``.
    """
    return contextvars.copy_context()


# --------------------------------------------------------------------------
# (c) queue workers in separate processes
# --------------------------------------------------------------------------
def inject_traceparent(payload: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Write the W3C ``traceparent`` into a job payload before enqueueing it.

    GOTCHA (c): the ingest workers are SEPARATE PROCESSES. No contextvar, no
    thread trick and no auto-instrumentation crosses a process boundary -- the
    only channel is the job payload itself. Without this, every ingest trace is
    orphaned: the enqueue span ends at the enqueue, and the work that matters
    (chunking, embedding, writing rows) shows up as thousands of unattached
    single-span traces that cannot be joined back to the request that caused them.

    Call at enqueue time; the same mapping is returned, mutated in place.
    """
    inject(payload)  # writes "traceparent" (and "tracestate" when present)
    return payload


def extract_traceparent(payload: Mapping[str, Any]) -> otel_context.Context:
    """Rebuild the parent context from a dequeued job payload.

    Use in the worker as::

        ctx = extract_traceparent(job)
        token = otel_context.attach(ctx)
        try:
            with stage("pace.ingest.chunk"):
                ...
        finally:
            otel_context.detach(token)

    or via :func:`worker_span`, which does exactly that.
    """
    return extract(payload)


@contextmanager
def worker_span(name: str, payload: Mapping[str, Any], **attrs: Any) -> Iterator[Span]:
    """Open ``name`` in the worker process, parented to the enqueuer's span.

    The counterpart to :func:`inject_traceparent`. Kind is CONSUMER so the trace
    UI renders the process hop as a queue link rather than a plain child call.
    """
    token = otel_context.attach(extract_traceparent(payload))
    try:
        with _tracer.start_as_current_span(name, kind=SpanKind.CONSUMER) as span:
            span.set_attribute(ATTR_STAGE, name)
            for key, value in attrs.items():
                if value is not None:
                    span.set_attribute(key, value)
            try:
                yield span
            except Exception as exc:  # noqa: BLE001 - recorded then re-raised
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
    finally:
        otel_context.detach(token)
