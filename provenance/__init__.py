"""PACE - Provenance-Aware Code Intelligence.

Retrieval over apache/airflow that answers "why is this code the way it is?"
with citations to commits, PRs, issues and inline review comments - or refuses,
explicitly, when the rationale was never written down anywhere.

Nothing heavy is imported here on purpose: `import provenance` must stay free of
database connections, model loads and OTel exporters so that `pace --help`, the
test collector and the CI lint job all work on a machine with no container
running and no "ml" extra installed.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
