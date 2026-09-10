"""HTTP surface for PACE.

``provenance.api.main:app`` is the ASGI app (``uvicorn provenance.api.main:app``).
``provenance.api.schemas`` holds the public request/response contract -- import
those models rather than hand-building dicts, so the CLI, the eval harness and
the API can never disagree about the shape of an answer.

Nothing is re-exported here on purpose: importing ``provenance.api`` must not
drag FastAPI into short CLI commands that only need the schemas.
"""
