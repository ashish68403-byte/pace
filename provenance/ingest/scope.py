"""Compute and FREEZE the corpus scope.

Failure this prevents
---------------------
Recall@10 = 0.62 means nothing unless you can say *over what*. Add 200 files and
recall moves without anything improving; drop 200 and it moves the other way.
So the file list is computed once, written to ``scope.txt``, committed, and then
treated as immutable: :func:`build_scope` refuses to silently overwrite a scope
file whose contents would change (pass ``force=True`` and bump the scope version
deliberately, which invalidates every previously reported recall number).

Second failure this prevents: the Apache licence header. Every Airflow source
file starts with the same ~16-line ASF header — roughly 9,700 byte-identical
lines spread over ~605 files. If the chunker hashes that text, small files
collapse onto one another and the header text forms a dense, uninformative
neighbourhood in the embedding index that every query partially matches. Hence
:func:`strip_licence_header`, which the chunker MUST call before hashing, and
the ``licence_header_lines`` figure in the report so the true corpus size is
honest.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from provenance.config import settings

__all__ = [
    "main",
    "DEFAULT_INCLUDE",
    "DEFAULT_EXCLUDE",
    "SCOPE_VERSION",
    "FileStats",
    "ScopeReport",
    "ScopeDriftError",
    "build_scope",
    "load_scope",
    "count_lines",
    "licence_header_span",
    "strip_licence_header",
]

# Bump ONLY when deliberately re-freezing. Every recall figure is scoped to this.
SCOPE_VERSION = "v1"

# ~60-80k physical lines of the core package. Deliberately excluded:
#   ui/          - TypeScript/React assets and generated API clients, not Python rationale
#   example_dags/- documentation-by-example; high lexical overlap, no interesting history
#   migrations/  - Alembic revisions are mechanical and dominate any lexical index
DEFAULT_INCLUDE: tuple[str, ...] = ("airflow-core/src/airflow/**/*.py",)
DEFAULT_EXCLUDE: tuple[str, ...] = (
    "**/ui/**",
    "**/example_dags/**",
    "**/migrations/**",
    "**/__pycache__/**",
    "**/*.pyi",
)

LICENCE_MARKER = "Licensed to the Apache Software Foundation"
# The ASF header is a leading comment block; a shebang / coding line may precede it.
_LICENCE_SCAN_LIMIT = 30
_PREAMBLE_RE = re.compile(r"^#!|^#.*coding[:=]")


class ScopeDriftError(RuntimeError):
    """Raised when a frozen scope.txt would change. Recall numbers are not comparable."""


@dataclass(slots=True)
class FileStats:
    path: str
    physical: int
    non_blank: int
    code: int  # non-blank, non-comment
    licence_header: int


@dataclass(slots=True)
class ScopeReport:
    scope_version: str
    corpus_path: str
    include: list[str]
    exclude: list[str]
    files: int = 0
    physical_lines: int = 0
    non_blank_lines: int = 0
    code_lines: int = 0
    licence_header_lines: int = 0
    per_file: list[FileStats] = field(default_factory=list)

    @property
    def licence_share(self) -> float:
        """Fraction of physical lines that are boilerplate. Sanity check: ~13%."""
        return self.licence_header_lines / self.physical_lines if self.physical_lines else 0.0

    def summary(self) -> str:
        return (
            f"scope {self.scope_version}: {self.files} files, "
            f"{self.physical_lines:,} physical / {self.non_blank_lines:,} non-blank / "
            f"{self.code_lines:,} code lines; "
            f"{self.licence_header_lines:,} licence-header lines "
            f"({self.licence_share:.1%} of physical, stripped before hashing)"
        )

    def to_json(self) -> str:
        payload = asdict(self)
        payload["licence_share"] = round(self.licence_share, 4)
        return json.dumps(payload, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- globs


def _match(rel_posix: str, pattern: str) -> bool:
    """Glob match where ``**`` may span zero directories.

    ``fnmatch``'s ``*`` already crosses ``/``, so ``**/ui/**`` behaves as intended.
    The one case it gets wrong is a ``/**/`` that should match zero segments:
    ``airflow-core/src/airflow/**/*.py`` must also match
    ``airflow-core/src/airflow/models.py``. So try the collapsed form too.
    """
    from fnmatch import fnmatchcase

    candidates = {pattern}
    while "/**/" in pattern:
        pattern = pattern.replace("/**/", "/", 1)
        candidates.add(pattern)
    return any(fnmatchcase(rel_posix, p) for p in candidates)


def _iter_candidates(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def select_paths(
    corpus_path: Path,
    include: Sequence[str] = DEFAULT_INCLUDE,
    exclude: Sequence[str] = DEFAULT_EXCLUDE,
) -> list[str]:
    """Return repo-relative POSIX paths in scope, sorted (the sort IS the freeze order)."""
    selected: list[str] = []
    for path in _iter_candidates(corpus_path):
        rel = path.relative_to(corpus_path).as_posix()
        if not any(_match(rel, p) for p in include):
            continue
        if any(_match(rel, p) for p in exclude):
            continue
        selected.append(rel)
    return sorted(selected)


# ----------------------------------------------------------------- licence header


def licence_header_span(text: str) -> int:
    """Number of leading lines occupied by the ASF licence header (0 if absent).

    The header is the contiguous run of ``#`` comment/blank lines at the top of the
    file that contains :data:`LICENCE_MARKER`, plus the single blank line that
    conventionally follows it.
    """
    lines = text.split("\n")
    head = lines[:_LICENCE_SCAN_LIMIT]
    if not any(LICENCE_MARKER in line for line in head):
        return 0

    index = 0
    # Skip shebang / coding cookie, which are not part of the licence block.
    while index < len(lines) and _PREAMBLE_RE.match(lines[index]):
        index += 1
    start = index
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("#") or stripped == "":
            index += 1
            continue
        break
    # Only count it if the marker really lives inside that block.
    if not any(LICENCE_MARKER in line for line in lines[start:index]):
        return 0
    return index


def strip_licence_header(text: str) -> tuple[str, int]:
    """Return ``(body, lines_removed)``.

    The chunker calls this BEFORE hashing and adds ``lines_removed`` back to every
    line number so reported ranges still address the real file on disk.
    """
    span = licence_header_span(text)
    if span == 0:
        return text, 0
    return "\n".join(text.split("\n")[span:]), span


# ------------------------------------------------------------------- line counting


def count_lines(text: str, *, path: str = "") -> FileStats:
    """Physical / non-blank / non-blank-non-comment counts plus licence-header lines.

    ``code`` is a lexical approximation: a line whose first non-space character is
    ``#``. It deliberately does NOT try to exclude docstrings — docstrings are
    rationale, which is the thing this project retrieves.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # a trailing newline is not a line
    physical = len(lines)
    non_blank = 0
    code = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        non_blank += 1
        if not stripped.startswith("#"):
            code += 1
    return FileStats(
        path=path,
        physical=physical,
        non_blank=non_blank,
        code=code,
        licence_header=licence_header_span(text),
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------------------ freezing


def build_scope(
    corpus_path: Path | None = None,
    out_path: Path | None = None,
    include: Sequence[str] = DEFAULT_INCLUDE,
    exclude: Sequence[str] = DEFAULT_EXCLUDE,
    *,
    write: bool = True,
    force: bool = False,
) -> ScopeReport:
    """Walk the corpus, apply globs, write ``scope.txt`` + ``scope_stats.json``.

    Raises :class:`ScopeDriftError` if a scope file already exists and the newly
    computed list differs, unless ``force=True``.
    """
    corpus = Path(corpus_path or settings.corpus_path)
    if not corpus.is_dir():
        raise FileNotFoundError(f"corpus not found at {corpus}; clone apache/airflow there")

    paths = select_paths(corpus, include, exclude)
    report = ScopeReport(
        scope_version=SCOPE_VERSION,
        corpus_path=str(corpus),
        include=list(include),
        exclude=list(exclude),
    )
    for rel in paths:
        stats = count_lines(_read(corpus / rel), path=rel)
        report.per_file.append(stats)
        report.files += 1
        report.physical_lines += stats.physical
        report.non_blank_lines += stats.non_blank
        report.code_lines += stats.code
        report.licence_header_lines += stats.licence_header

    if write:
        target = Path(out_path) if out_path else default_scope_path()
        _write_frozen(target, paths, force=force)
        target.with_name("scope_stats.json").write_text(report.to_json(), encoding="utf-8")
    return report


def _write_frozen(target: Path, paths: Iterable[str], *, force: bool) -> None:
    body = "\n".join(paths) + "\n"
    if target.exists() and not force:
        existing = target.read_text(encoding="utf-8")
        if existing != body:
            old = set(existing.split()) - set(body.split())
            new = set(body.split()) - set(existing.split())
            raise ScopeDriftError(
                f"{target} is frozen but the computed scope differs "
                f"(-{len(old)} +{len(new)} files). Recall numbers measured on the old "
                f"scope are NOT comparable to the new one. Re-freeze with force=True "
                f"and bump SCOPE_VERSION if that is what you mean."
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def default_scope_path() -> Path:
    """``<repo>/scope.txt`` — committed, next to pyproject.toml."""
    return _repo_root() / "scope.txt"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[2]


def load_scope(path: Path | None = None) -> list[str]:
    """Read the frozen file list. Every ingest/eval run reads this, never re-globs."""
    target = Path(path) if path else default_scope_path()
    if not target.exists():
        raise FileNotFoundError(f"{target} missing — run `pace scope build` first")
    return [
        line.strip()
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


# ------------------------------------------------------------------------- cli


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pace scope",
        description=(
            "Select and FREEZE the subset of the corpus under study "
            "(~60-80k lines, not the whole tree)."
        ),
        epilog=(
            "The frozen list is what every recall number is scoped to. Re-freezing with "
            "--force invalidates every previously reported figure, which is why it is not "
            "the default and why SCOPE_VERSION should be bumped in the same commit."
        ),
    )
    parser.add_argument(
        "--corpus", default=None, help="corpus path (default: settings.corpus_path)"
    )
    parser.add_argument(
        "--out", default=None, help=f"scope file to write (default: {default_scope_path()})"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="recompute and compare against the frozen file without writing; exit 1 on drift",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-freeze even though the computed scope differs (invalidates past recall numbers)",
    )
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Thin CLI adapter for `pace scope`.

    Default behaviour is to build and freeze. A drift against an existing frozen
    file is an error, not a silent overwrite: the file list is the denominator of
    every recall figure in the project, so changing it without saying so makes two
    runs incomparable while both still print a number.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(list(argv) if argv is not None else None)
    corpus = Path(args.corpus) if args.corpus else None
    target = Path(args.out) if args.out else default_scope_path()

    try:
        report = build_scope(
            corpus_path=corpus,
            out_path=target,
            write=not args.check,
            force=args.force,
        )
    except FileNotFoundError as exc:
        print(str(exc))
        return 2
    except ScopeDriftError as exc:
        print(str(exc))
        return 1

    if args.check:
        try:
            frozen = load_scope(target)
        except FileNotFoundError:
            print(f"{target} does not exist yet; run `pace scope` to freeze it")
            return 1
        computed = [stats.path for stats in report.per_file]
        if frozen != computed:
            missing = sorted(set(frozen) - set(computed))
            added = sorted(set(computed) - set(frozen))
            print(
                f"scope drift against {target}: -{len(missing)} +{len(added)} files. "
                "Recall numbers measured on the frozen scope are not comparable to this tree."
            )
            for path in missing[:10]:
                print(f"  - {path}")
            for path in added[:10]:
                print(f"  + {path}")
            return 1
        print(f"scope matches {target} ({len(frozen)} files)")
        return 0

    print(report.to_json() if args.json else report.summary())
    if not args.json:
        print(f"frozen -> {target}")
    return 0
