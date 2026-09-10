"""Observability for PACE.

One request must produce ONE complete trace, and the trace id must come back in
the response body. Without that, debugging a multi-stage retrieval pipeline
degenerates into print-statement archaeology across processes.

Public surface:
    setup_tracing(app)      -- install the tracer provider + FastAPI instrumentation
    current_trace_id()      -- 32-hex trace id of the active span, or None
    stage(name, **attrs)    -- contextmanager emitting a canonically-named span
    SPAN_*                  -- the span-name constants the whole pipeline shares
"""

from provenance.obs.otel import current_trace_id, setup_tracing
from provenance.obs.spans import (
    SPAN_AGENT_STEP,
    SPAN_EMBED_QUERY,
    SPAN_FUSE_RRF,
    SPAN_PARSE_QUERY,
    SPAN_QUERY,
    SPAN_RERANK,
    SPAN_SEARCH_DENSE,
    SPAN_SEARCH_LEXICAL,
    SPAN_SYNTHESIZE,
    SPAN_VERIFY,
    extract_traceparent,
    inject_traceparent,
    run_in_thread_with_context,
    stage,
    tool_span_name,
)

__all__ = [
    "SPAN_AGENT_STEP",
    "SPAN_EMBED_QUERY",
    "SPAN_FUSE_RRF",
    "SPAN_PARSE_QUERY",
    "SPAN_QUERY",
    "SPAN_RERANK",
    "SPAN_SEARCH_DENSE",
    "SPAN_SEARCH_LEXICAL",
    "SPAN_SYNTHESIZE",
    "SPAN_VERIFY",
    "current_trace_id",
    "extract_traceparent",
    "inject_traceparent",
    "run_in_thread_with_context",
    "setup_tracing",
    "stage",
    "tool_span_name",
]
