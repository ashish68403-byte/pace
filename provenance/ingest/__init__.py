"""Corpus ingestion: scope freezing, git history walking, and the week-2 skeleton.

What this package prevents:

* An un-frozen corpus. Recall@k is only comparable within one fixed file list, so
  the scope is computed once, written to ``scope.txt`` and treated as immutable
  (see :mod:`provenance.ingest.scope`).
* A truncated history. apache/airflow moved ``airflow/`` -> ``airflow-core/src/airflow/``
  on 2025-03-21; a path-scoped or shallow walk silently loses ~76% of the commits
  that explain the code (see :mod:`provenance.ingest.git_walk`).
"""

from __future__ import annotations

__all__ = ["scope", "git_walk", "skeleton"]
