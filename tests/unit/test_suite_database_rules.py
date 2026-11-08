"""Slice C1: asked to run in `pace`, the suite refuses before any test (tracker 006 row 9).

A subprocess, because the refusal happens while pytest starts: no test inside this session could
observe it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COLLECT = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
REAL = "postgresql://postgres:pace@localhost:5432/pace"


def test_the_suite_refuses_to_run_in_the_real_database():
    done = subprocess.run(
        [*COLLECT, "tests/unit/test_metrics.py"],
        cwd=ROOT,
        env={**os.environ, "PACE_TEST_DATABASE_URL": REAL},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == pytest.ExitCode.USAGE_ERROR, done.stdout + done.stderr
    assert "PACE_TEST_DATABASE_URL" in done.stderr
