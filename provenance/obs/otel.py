"""OpenTelemetry wiring: tracer provider, OTLP/HTTP exporter, FastAPI instrumentation.

Failure this prevents: a pipeline with a lexical search, a dense search, a fusion
step, an agent loop and a verifier is impossible to debug from logs alone. One
trace per request, exported over OTLP/HTTP to whatever collector is listening on
``settings.otel_endpoint`` (Jaeger, Tempo, otel-collector -- all speak 4318), plus
the trace id echoed in the response body so a user-reported bad answer maps to an
exact trace.

Idempotent by design: ``setup_tracing`` is called from the FastAPI lifespan AND
from CLI entrypoints, and OpenTelemetry only honours the FIRST
``set_tracer_provider`` call -- a second one logs a warning and is ignored, which
would silently leave the second caller unexported. We therefore guard it.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import format_trace_id

from provenance.config import settings

log = logging.getLogger(__name__)

# OTLP/HTTP wants the full signal path; settings.otel_endpoint is the base root.
_TRACES_PATH = "/v1/traces"

_configured = False


def code_version() -> str:
    """Version of the running code, for the resource attribute.

    Installed distribution version when available; otherwise a marker, so a trace
    from an editable checkout is never mistaken for a released build.
    """
    try:
        return version("pace")
    except PackageNotFoundError:  # editable / source checkout
        return "0.0.0+dev"


def build_resource() -> Resource:
    """Resource attributes attached to every span exported by this process."""
    return Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": code_version(),
            # Derivation provenance: which model/chunker produced the artifacts this
            # process reads. Makes "the eval regressed" answerable from traces alone.
            "pace.embedding_model_version": settings.embedding_model_version,
            "pace.chunking_strategy_version": settings.chunking_strategy_version,
        }
    )


def setup_tracing(app: Any | None = None) -> TracerProvider:
    """Configure the global tracer provider and instrument ``app`` if given.

    Safe to call repeatedly; only the first call installs a provider. Passing a
    FastAPI app applies auto-instrumentation, which gives a server span per HTTP
    request and propagates inbound ``traceparent`` headers.
    """
    global _configured

    provider = trace.get_tracer_provider()
    if not _configured:
        provider = TracerProvider(resource=build_resource())
        exporter = OTLPSpanExporter(endpoint=settings.otel_endpoint.rstrip("/") + _TRACES_PATH)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _configured = True
        log.debug("tracing configured -> %s", settings.otel_endpoint)

    if app is not None:
        # Imported lazily: the instrumentation package is only needed for the API
        # process, and importing it pulls in the whole ASGI middleware stack.
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # excluded_urls keeps the healthcheck out of the trace store; a 1Hz probe
        # otherwise buries every real request.
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=trace.get_tracer_provider(), excluded_urls="healthz"
        )

    return provider  # type: ignore[return-value]


def get_tracer(name: str = "provenance") -> trace.Tracer:
    """Tracer for a module. Cheap; no need to cache at call sites."""
    return trace.get_tracer(name)


def current_trace_id() -> str | None:
    """32-hex trace id of the active span, or None when nothing is recording.

    Returned to clients in the response body (see ``provenance.api.schemas``), so
    a bug report carries its own trace pointer.
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid or ctx.trace_id == 0:
        return None
    return format_trace_id(ctx.trace_id)


def shutdown_tracing() -> None:
    # Reset the install-once flag: setup_tracing() short-circuits on it, so
    # without this a second setup_tracing() in the same process silently
    # reuses the provider we just shut down, current_trace_id() returns None,
    # and run_demo raises "no recording span". Bites the first in-process
    # e2e test of `pace demo`, never the CLI.
    global _configured
    """Flush pending spans. Call before a short-lived CLI process exits.

    BatchSpanProcessor exports on a timer; a CLI run that finishes in 200ms would
    otherwise drop its entire trace on the floor.
    """
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        shutdown()
    _configured = False
