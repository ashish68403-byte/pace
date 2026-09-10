"""`pace` command line entry point -- the single authority on the command surface.

Canonical commands, and nothing else claims to exist:

    pace demo         walking skeleton: one request, one complete trace
    pace ingest       walk git history into the graph
    pace scope        select/freeze the ~60-80k-line subset under study
    pace eval         run | fixtures | invariants
    pace leakage      substring-search baseline over the golden set
    pace health       database + extension healthcheck
    pace version      versions in effect

Two properties this module exists to hold, both of which were broken:

1. **`pace --help` works on a fresh clone** with no database, no corpus and no
   "ml" extra. Every heavy dependency is imported inside a command body. The one
   module imported at import time is `provenance.eval.runner`, because Typer's
   `add_typer` needs the sub-app object to exist at registration time -- and that
   import is wrapped, so a partial install degrades `pace eval` to a readable
   error instead of taking `pace --help` down with it.
2. **Every sub-command is wired to an entry point that exists and whose return
   value is honoured.** `pace demo` pointed at `run_demo`, bound a list to a
   `str` parameter and threw away the `DemoResult` -- so the walking skeleton's
   report was never printed and the command still exited 0. `pace eval` used a
   shim that dropped argv, so Click re-read `sys.argv[1:]`, still saw the word
   `eval`, and every invocation died with "No such command 'eval'".
"""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(
    name="pace",
    help="PACE - provenance-aware code intelligence over apache/airflow.",
    no_args_is_help=True,
    add_completion=False,
)

# Sub-commands pass their unparsed arguments straight through to the owning
# module, which is where those flags are defined and documented.
PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


# --------------------------------------------------------------------------- #
# Lazy entry-point resolution
# --------------------------------------------------------------------------- #
def _load(module_path: str, attr: str) -> Callable[..., Any]:
    """Import `module_path` and return its `attr`, or exit with a usable message."""
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:  # missing optional dep, or module not built yet
        typer.secho(
            f"cannot run this command: {module_path} is unavailable ({exc}).\n"
            "If this is a dependency error, try `uv sync` (or `uv sync --extra ml` "
            "for embedding commands).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2) from exc

    entry = getattr(module, attr, None)
    if entry is None:
        typer.secho(f"{module_path} has no entry point '{attr}'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    return entry  # type: ignore[no-any-return]


def _load_script(relative: str, attr: str) -> Callable[..., Any]:
    """Load an entry point from a standalone script that is not on sys.path.

    Only resolvable in a source checkout: `scripts/` is not inside the wheel
    (`[tool.hatch.build.targets.wheel] packages = ["provenance"]`), so under a
    non-editable install this path does not exist, and the message has to say
    that rather than report a mysterious missing file.
    """
    import importlib.util

    path = Path(__file__).resolve().parent.parent / relative
    if not path.is_file():
        typer.secho(
            f"{path} not found. `pace leakage` runs from a source checkout; "
            "scripts/ is not packaged into the wheel.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        typer.secho(f"cannot load {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return _load_attr(module, attr, str(path))


def _load_attr(module: Any, attr: str, where: str) -> Callable[..., Any]:
    entry = getattr(module, attr, None)
    if entry is None:
        typer.secho(f"{where} has no entry point '{attr}'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    return entry  # type: ignore[no-any-return]


def _call(entry: Callable[..., Any], argv: Sequence[str]) -> None:
    """Call an entry point, forward argv, and honour what it returns.

    Three separate regressions are fenced off here, and every one of them exited
    0 while doing nothing:

    * argv was passed positionally to anything that had *a* parameter, so
      `pace demo --path x` bound the LIST `['--path', 'x']` to `run_demo`'s
      `path: str | None`. Entry points now either take `argv` or take nothing.
    * a non-int return value was silently discarded. `run_demo` returns a
      `DemoResult`; discarding it meant `format_report()` never printed and the
      walking skeleton produced no output at all. A non-int return now means the
      command is pointed at the wrong function, and says so.
    * arguments handed to a zero-argument entry point vanished without a word.
    """
    args = list(argv)
    accepts_argv = bool(inspect.signature(entry).parameters)

    if not accepts_argv and args:
        typer.secho(
            f"this command takes no arguments, but got {args}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    result = entry(args) if accepts_argv else entry()

    if result is None:
        return
    if isinstance(result, int):
        if result != 0:
            raise typer.Exit(code=int(result))
        return
    typer.secho(
        f"internal error: {entry.__module__}:{entry.__name__} returned "
        f"{type(result).__name__}, not an exit code. This command is pointed at "
        "the wrong entry point and its output is being thrown away.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=2)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@app.command(context_settings=PASSTHROUGH)
def demo(ctx: typer.Context) -> None:
    """Walking skeleton: one request, one complete trace, trace_id in the response."""
    # `main`, not `run_demo`: main() is the CLI adapter that prints
    # format_report(...) and returns an exit code. run_demo() returns a
    # DemoResult, which this layer has no way to render.
    _call(_load("provenance.ingest.skeleton", "main"), ctx.args)


@app.command(context_settings=PASSTHROUGH)
def ingest(ctx: typer.Context) -> None:
    """Walk the git history of the corpus and load commits, chunks and evidence."""
    _call(_load("provenance.ingest.git_walk", "main"), ctx.args)


@app.command(context_settings=PASSTHROUGH)
def scope(ctx: typer.Context) -> None:
    """Select the subset of the repo under study (~60-80k lines, not the whole tree)."""
    _call(_load("provenance.ingest.scope", "main"), ctx.args)


# --- eval: mounted as a real Typer sub-app ---------------------------------- #
# `typer.add_typer(runner.app, name="eval")` is the canonical mount. The
# alternative -- a passthrough command that calls runner.main() -- dropped argv,
# so Click re-parsed sys.argv[1:], which still began with the word "eval", and
# `pace eval run` died with "No such command 'eval'". A CLI entry point that
# re-reads global state cannot be composed; do not reintroduce that shim.
#
# add_typer needs the sub-app object now, so runner is the one module imported at
# import time. Its top-level imports are all base dependencies (numpy, psycopg,
# typer) and none of them open a connection -- but guarding the import keeps rule
# 1 of this module's docstring true even on a half-installed venv: a missing
# dependency must cost you `pace eval`, never `pace --help`.
try:
    from provenance.eval.runner import app as _eval_app
except ImportError as _exc:  # pragma: no cover - only on a partial install
    _EVAL_UNAVAILABLE: str | None = str(_exc)

    @app.command(name="eval", context_settings=PASSTHROUGH)
    def eval_(ctx: typer.Context) -> None:
        """Run an eval suite. Unavailable: the eval dependencies are not installed."""
        typer.secho(
            "`pace eval` is unavailable: provenance.eval.runner will not import "
            f"({_EVAL_UNAVAILABLE}).\nTry `uv sync` (add `--extra eval` for the "
            "parquet per-query table).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

else:
    _EVAL_UNAVAILABLE = None
    app.add_typer(_eval_app, name="eval")


@app.command(context_settings=PASSTHROUGH)
def leakage(ctx: typer.Context) -> None:
    """Substring-search baseline over the golden set; must score below 0.35 recall@10."""
    try:
        entry = _load("provenance.eval.leakage", "main")
    except typer.Exit:
        entry = _load_script("scripts/leakage_probe.py", "main")
    _call(entry, ctx.args)


@app.command()
def health() -> None:
    """Database and extension healthcheck: server version, extensions, migration."""
    import psycopg

    from provenance.graph.db import REQUIRED_EXTENSIONS, healthcheck

    try:
        # Narrow, not blanket. psycopg.Error covers every server-side and
        # connection failure, and psycopg_pool's PoolTimeout / PoolClosed
        # subclass psycopg.OperationalError. A TypeError or KeyError out of
        # healthcheck() is a row-shape bug in db.py, and reporting that as
        # "database unreachable" is precisely how a perfectly healthy database
        # came to be reported as degraded (audit #1). Let it traceback.
        report = healthcheck()
    except (psycopg.Error, OSError) as exc:
        typer.secho(
            "database unreachable at the configured PACE_DATABASE_URL: "
            f"{type(exc).__name__}: {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc

    extensions: dict[str, str] = report["extensions"]
    missing: list[str] = report["missing_extensions"]
    revision = report["alembic_version"]

    typer.echo(f"server        PostgreSQL {report['server_version']}")
    typer.echo(f"database      {report['database']} as {report['username']}")
    for name in REQUIRED_EXTENSIONS:
        installed = extensions.get(name)
        if installed is None:
            typer.secho(f"extension     {name:<12} MISSING", fg=typer.colors.RED)
        else:
            typer.echo(f"extension     {name:<12} {installed}")
    typer.echo(f"alembic       {revision or 'NONE - run `alembic upgrade head`'}")
    typer.echo(f"pool          min={report['pool']['min_size']} max={report['pool']['max_size']}")

    # Exit non-zero on both failure modes, because scripts/bootstrap.sh gates on
    # this command: a database missing an extension and a database that was never
    # migrated are equally unusable, and both otherwise print a cheerful report
    # and exit 0.
    if missing:
        typer.secho(f"missing extensions: {', '.join(missing)}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if revision is None:
        typer.secho(
            "no alembic_version table: this database has never been migrated.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the installed version and the identity/derivation versions in effect."""
    from provenance import __version__
    from provenance.config import settings

    typer.echo(f"pace {__version__}")
    typer.echo(f"chunking_strategy_version={settings.chunking_strategy_version}")
    typer.echo(f"embedding_model_version={settings.embedding_model_version}")
    typer.echo(f"embedding_dim={settings.embedding_dim}")
    typer.echo(f"embedding_backend={settings.embedding_backend}")


if __name__ == "__main__":
    app()
