"""The two new commands exist and describe themselves, with no database needed."""

from __future__ import annotations

from typer.testing import CliRunner

from provenance.cli import app


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
