"""Slice C1: the suite runs in its own database, never in `pace` (tracker 006 row 9)."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from provenance.config import settings

pytestmark = pytest.mark.db


def test_the_suite_never_runs_in_the_real_database(migrated_database):
    (name,) = migrated_database.execute("SELECT current_database()").fetchone()
    assert name != "pace"
    assert urlsplit(settings.database_url).path.lstrip("/") == name  # the code under test agrees
