"""The two new commands exist and describe themselves, with no database needed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from provenance.cli import app

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "leakage_probe.py"


def test_pace_measure_lists_truncation():
    result = CliRunner().invoke(app, ["measure", "--help"])
    assert result.exit_code == 0
    assert "truncation" in result.output


def test_pace_history_has_help():
    result = CliRunner().invoke(app, ["history", "--help"])
    assert result.exit_code == 0
    assert "current-path" in result.output


def test_pace_pool_lists_build():
    result = CliRunner().invoke(app, ["pool", "--help"])
    assert result.exit_code == 0
    assert "build" in result.output


def test_pace_measure_lists_pool():
    result = CliRunner().invoke(app, ["measure", "--help"])
    assert result.exit_code == 0
    assert "pool" in result.output


def _probe_args(tmp_path: Path) -> list[str]:
    """A one-document fixture pool and an empty golden set: nothing is scored."""
    pool = tmp_path / "pool.jsonl"
    pool.write_text('{"chunk_id": "d1", "content": "Fix the scheduler"}\n', encoding="utf-8")
    golden = tmp_path / "none.jsonl"  # missing, so load_golden_set returns []
    return ["--pool", str(pool), "--golden", str(golden), "--no-dense", "--allow-inconclusive"]


def test_pace_leakage_runs_the_packaged_probe(tmp_path):
    result = CliRunner().invoke(app, ["leakage", *_probe_args(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "VERDICT: INCONCLUSIVE" in result.output
    assert "unavailable" not in result.output  # no failed import before a fallback


def test_the_ci_script_path_still_runs_the_probe(tmp_path):
    done = subprocess.run(
        [sys.executable, str(SCRIPT), *_probe_args(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "VERDICT: INCONCLUSIVE" in done.stdout


def test_pace_golden_lists_generate():
    result = CliRunner().invoke(app, ["golden", "--help"])
    assert result.exit_code == 0
    assert "generate" in result.output


def test_pace_github_lists_backfill():
    result = CliRunner().invoke(app, ["github", "--help"])
    assert result.exit_code == 0
    assert "backfill" in result.output


def test_pace_audit_lists_its_four_commands():
    result = CliRunner().invoke(app, ["audit", "--help"])
    assert result.exit_code == 0
    for name in ("sample", "label", "relabel", "report"):
        assert name in result.output
