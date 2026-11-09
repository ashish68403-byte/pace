"""Who owns each line of a function, and what `git log -L` lists for it (slice C2).

ADR-0005 §2 and §6, and its addendum of 2026-11-09 (docs/tasks/symbols-blame/001_blame_arms.md):
arm A is `git blame -w -M --first-parent` with checkpoint A's mechanical commits ignored, and
arm L is `git log --first-parent -L`. Both follow first parents, because checkpoint A's graph
-- and so every gold commit -- is the first-parent line.

Three layers:

* pure parsers: blame's porcelain output into one owner per line, those lines into the
  longest ranges that share both owners, and `git log -L` output into commits;
* git runners that know nothing about the database, so slice C3 can run `line_log` on
  CodeShovel's projects;
* the build, which blames each file twice (without and with the mechanical list) and logs
  each function, one file per transaction, into `blame_ranges` and `line_log_commits`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from provenance.config import settings
from provenance.eval.schema import load_golden_set, passes_leakage_filter
from provenance.graph.tables import BLAME_RANGES, COMMITS, FILES, LINE_LOG_COMMITS
from provenance.ingest.git_walk import _assert_not_shallow
from provenance.parse.symbols import symbol_spans

#: Plain blame. `--first-parent`: a side branch's change belongs to its merge, as the gold
#: does. `-w`: whitespace changes own nothing. `-M`: lines moved within the file keep their
#: owner. No `-C` (ADR-0005 §6).
BLAME_FLAGS: Final = ("--porcelain", "-w", "-M", "--first-parent")
#: `git log -L`, one SHA per listed commit and no patch.
LOG_FLAGS: Final = ("--first-parent", "--format=%H", "-s")
#: `--ignore-revs-file ""` empties the list read so far, including one the repository's own
#: config sets (git-blame(1)), so plain blame stays plain. `-c blame.ignoreRevsFile=` does
#: not clear it (checked 2026-11-09).
_NO_CONFIGURED_IGNORES: Final = ("--ignore-revs-file", "")

_HEADER = re.compile(r"([0-9a-f]{40}) (\d+) (\d+)(?: \d+)?")
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class LineOwner:
    """Line `line` of the blamed file, written by `commit` as line `orig_line` of `path`."""

    line: int
    commit: str
    path: str
    orig_line: int


@dataclass(frozen=True)
class OwnerRange:
    """Lines `start` to `stop - 1` that share both owners: one `blame_ranges` row.

    `commit`, `path` and `orig_line` (for `start`) are plain blame's. `effective` is the owner
    with mechanical commits ignored, or None when that is still a mechanical commit.
    """

    start: int
    stop: int
    commit: str
    path: str
    orig_line: int
    effective: str | None


def parse_porcelain(text: str) -> list[LineOwner]:
    """`git blame --porcelain` output as one owner per line, in output order.

    Each line's header is `<sha> <orig_line> <line> [<lines in group>]`, then the line itself
    after a tab. A commit's details, `filename` among them, come the first time it appears;
    later lines of that commit take the path last given for it.
    """
    owners: list[LineOwner] = []
    paths: dict[str, str] = {}
    current: tuple[str, int, int] | None = None
    for raw in text.split("\n"):
        if raw.startswith("\t"):
            if current is None:
                raise ValueError("blame output has a line before any header")
            commit, orig_line, line = current
            if commit not in paths:
                raise ValueError(f"blame output gives no filename for {commit}")
            owners.append(LineOwner(line, commit, paths[commit], orig_line))
            current = None
        elif header := _HEADER.fullmatch(raw):
            current = (header[1], int(header[2]), int(header[3]))
        elif raw.startswith("filename "):
            name = raw.removeprefix("filename ")
            if name.startswith('"'):
                raise ValueError(f"blame output quotes a path this parser cannot read: {name}")
            if current is None:
                raise ValueError("blame output has a filename before any header")
            paths[current[0]] = name
    return owners


def owner_ranges(
    plain: Sequence[LineOwner], filtered: Sequence[LineOwner], mechanical: Collection[str]
) -> list[OwnerRange]:
    """Merge one file's lines into the longest runs that share both owners.

    A run continues while the lines are consecutive, the plain owner and its path stay the
    same with consecutive original lines, and the filtered owner stays the same. A filtered
    owner that is still mechanical -- git cannot re-attribute a line an ignored commit added
    -- is stored as None.
    """
    kept = {owner.line: owner.commit for owner in filtered}
    if set(kept) != {owner.line for owner in plain}:
        raise ValueError("the plain and the filtered blame cover different lines")
    ranges: list[OwnerRange] = []
    for owner in sorted(plain, key=lambda found: found.line):
        effective = None if kept[owner.line] in mechanical else kept[owner.line]
        last = ranges[-1] if ranges else None
        if (
            last is not None
            and last.stop == owner.line
            and (last.commit, last.path, last.effective) == (owner.commit, owner.path, effective)
            and last.orig_line + (last.stop - last.start) == owner.orig_line
        ):
            ranges[-1] = replace(last, stop=owner.line + 1)
        else:
            ranges.append(
                OwnerRange(
                    owner.line, owner.line + 1, owner.commit, owner.path, owner.orig_line, effective
                )
            )
    return ranges


def parse_line_log(text: str) -> list[str]:
    """`git log -L --format=%H -s` output as commits, newest first, each once."""
    commits: dict[str, None] = {}
    for raw in text.split("\n"):
        token = raw.strip()
        if _SHA.fullmatch(token):
            commits.setdefault(token, None)
    return list(commits)


def blame_algo(git_version: str, mechanical: int, digest: str) -> str:
    """What made a `blame_ranges` row, stored with it so rows from two setups never mix."""
    return (
        f"git {git_version} blame {' '.join(BLAME_FLAGS)}; filtered with --ignore-revs-file: "
        f"{mechanical} mechanical commits, sha256 {digest}"
    )


def log_algo(git_version: str) -> str:
    """What made a `line_log_commits` row."""
    return f"git {git_version} log {' '.join(LOG_FLAGS)} -L <span>:<path>"


class GitFailed(RuntimeError):
    """git exited non-zero; the message carries its own words."""


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo` with global and system config switched off."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if done.returncode != 0:
        raise GitFailed(f"git {args[0]} failed ({done.returncode}): {done.stderr.strip()[:500]}")
    return done.stdout


def git_version() -> str:
    """`2.43.0`, from `git version 2.43.0`."""
    done = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
    return done.stdout.strip().removeprefix("git version ").strip()


def show(repo: Path, rev: str, path: str) -> str:
    """A file's text at a revision."""
    return _git(repo, "show", f"{rev}:{path}")


def blame_lines(
    repo: Path,
    rev: str,
    path: str,
    spans: Sequence[tuple[int, int]],
    *,
    ignore_revs: Path | None = None,
) -> list[LineOwner]:
    """One owner per line of `spans` in `path` at `rev`: one call, one `-L` per span.

    One call per file gives every line the owner one call per span would: on the three
    Airflow files with the most functions, none of 4,573 lines differed (2026-11-09).
    """
    args = ["blame", *BLAME_FLAGS, *_NO_CONFIGURED_IGNORES]
    if ignore_revs is not None:
        args += ["--ignore-revs-file", str(ignore_revs.resolve())]
    for first, last in spans:
        args += ["-L", f"{first},{last}"]
    return parse_porcelain(_git(repo, *args, rev, "--", path))


def line_log(repo: Path, rev: str, path: str, spans: Sequence[tuple[int, int]]) -> list[str]:
    """Every commit `git log --first-parent -L` lists for `spans` of `path` at `rev`."""
    args = ["log", *LOG_FLAGS]
    for first, last in spans:
        args += ["-L", f"{first},{last}:{path}"]
    return parse_line_log(_git(repo, *args, rev))


def renames(repo: Path, commit: str) -> dict[str, str]:
    """`{new_path: old_path}` for the files `commit` renamed, against its first parent."""
    found: dict[str, str] = {}
    for raw in _git(repo, "diff", "-M", "--name-status", f"{commit}^", commit).split("\n"):
        fields = raw.split("\t")
        if len(fields) == 3 and fields[0].startswith("R"):
            found[fields[2]] = fields[1]
    return found


class BlameRefused(RuntimeError):
    """The build will not run, or not on this file; nothing was written for the file."""


@dataclass(frozen=True)
class Target:
    """The functions to blame: what the questions name, at the commit they are pinned to."""

    pinned: str
    questions: int
    functions: tuple[tuple[str, str], ...]  # (path, qualified_name), sorted
    source_sha256: str = ""


def golden_target(path: Path) -> Target:
    """The retained dev records' functions (ADR-0005 §1); the test split stays sealed."""
    records = [r for r in load_golden_set(path) if r.split == "dev" and passes_leakage_filter(r)]
    if not records:
        raise BlameRefused(f"{path} holds no retained dev record")
    pins = {record.corpus_commit for record in records}
    if len(pins) != 1 or None in pins:
        raise BlameRefused(f"the retained dev records are pinned to {len(pins)} commits, not one")
    found: set[tuple[str, str]] = set()
    for record in records:
        anchors = (
            *record.required_evidence.symbol_anchors,
            *record.supporting_evidence.symbol_anchors,
        )
        for anchor in anchors:
            if anchor.qualified_name:
                found.add((anchor.path, anchor.qualified_name))
    return Target(
        pinned=str(pins.pop()),
        questions=len(records),
        functions=tuple(sorted(found)),
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


@dataclass(frozen=True)
class BuildResult:
    files_blamed: int = 0
    files_skipped: int = 0
    blame_rows: int = 0
    functions_logged: int = 0
    functions_skipped: int = 0
    log_rows: int = 0

    def summary(self) -> str:
        return (
            f"blame: {self.files_blamed} files blamed, {self.files_skipped} already stored, "
            f"{self.blame_rows} rows; git log -L: {self.functions_logged} functions logged, "
            f"{self.functions_skipped} already stored, {self.log_rows} rows"
        )


_COMMIT_KNOWN = f"SELECT 1 FROM {COMMITS} WHERE repo_id = %s AND commit_sha = %s"
_MECHANICAL = (
    f"SELECT commit_sha FROM {COMMITS} WHERE repo_id = %s AND is_mechanical ORDER BY commit_sha"
)
_FILE_IDS = f"SELECT path, file_id FROM {FILES} WHERE repo_id = %s AND path = ANY(%s)"
_ALGOS = {
    BLAME_RANGES: f"SELECT DISTINCT blame_algo FROM {BLAME_RANGES} "
    "WHERE repo_id = %s AND snapshot_sha = %s",
    LINE_LOG_COMMITS: f"SELECT DISTINCT algo FROM {LINE_LOG_COMMITS} "
    "WHERE repo_id = %s AND snapshot_sha = %s",
}
_DELETE_SNAPSHOT = "DELETE FROM {} WHERE repo_id = %s AND snapshot_sha = %s"
_STORED_RANGES = (
    f"SELECT lower(line_range), upper(line_range) FROM {BLAME_RANGES} "
    "WHERE file_id = %s AND snapshot_sha = %s"
)
_DELETE_FILE_RANGES = f"DELETE FROM {BLAME_RANGES} WHERE file_id = %s AND snapshot_sha = %s"
_INSERT_RANGE = (
    f"INSERT INTO {BLAME_RANGES} (repo_id, file_id, snapshot_sha, line_range, orig_commit_sha, "
    "orig_path, orig_line_start, effective_commit_sha, blame_algo) "
    "VALUES (%s, %s, %s, %s::int4range, %s, %s, %s, %s, %s)"
)
_LOG_STORED = (
    f"SELECT 1 FROM {LINE_LOG_COMMITS} WHERE repo_id = %s AND file_id = %s "
    "AND snapshot_sha = %s AND qualified_name = %s LIMIT 1"
)
_INSERT_LOG = (
    f"INSERT INTO {LINE_LOG_COMMITS} "
    "(repo_id, file_id, snapshot_sha, qualified_name, commit_sha, algo) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)


def file_ids(conn: Any, repo_id: int, paths: Sequence[str]) -> dict[str, int]:
    """`files.file_id` for each path; refuses when one has no row."""
    found = {
        str(path): int(file_id) for path, file_id in conn.execute(_FILE_IDS, (repo_id, list(paths)))
    }
    missing = [path for path in paths if path not in found]
    if missing:
        raise BlameRefused(
            f"{len(missing)} path(s) have no files row, first {missing[0]}: run `pace ingest`"
        )
    return found


@dataclass
class _Run:
    """What every file of one build shares."""

    conn: Any
    repo: Path
    repo_id: int
    pinned: str
    ignore: Path
    mechanical: frozenset[str]
    made_by: str
    logged_by: str
    counts: Counter[str] = field(default_factory=Counter)

    def file(self, path: str, file_id: int, names: Sequence[str]) -> None:
        """Blame one file's needed lines twice, unless they are stored, then log each function."""
        spans = symbol_spans(show(self.repo, self.pinned, path), path) or {}
        missing = [name for name in names if name not in spans]
        if missing:
            raise BlameRefused(
                f"{path}: {missing[0]!r} is not a function at {self.pinned[:10]}; "
                "is the golden set stale?"
            )
        needed = sorted({span for name in names for span in spans[name]})
        lines = {line for first, last in needed for line in range(first, last + 1)}
        stored = {
            line
            for low, high in self.conn.execute(_STORED_RANGES, (file_id, self.pinned))
            for line in range(int(low), int(high))
        }
        if lines <= stored:
            self.counts["files_skipped"] += 1
        else:
            self._blame(path, file_id, needed, lines)
        for name in names:
            key = (self.repo_id, file_id, self.pinned, name)
            if self.conn.execute(_LOG_STORED, key).fetchone():
                self.counts["functions_skipped"] += 1
                continue
            commits = line_log(self.repo, self.pinned, path, spans[name])
            with self.conn.cursor() as cur:
                cur.executemany(
                    _INSERT_LOG,
                    [
                        (self.repo_id, file_id, self.pinned, name, sha, self.logged_by)
                        for sha in commits
                    ],
                )
            self.counts["functions_logged"] += 1
            self.counts["log_rows"] += len(commits)

    def _blame(
        self, path: str, file_id: int, spans: Sequence[tuple[int, int]], lines: set[int]
    ) -> None:
        plain = blame_lines(self.repo, self.pinned, path, spans)
        filtered = blame_lines(self.repo, self.pinned, path, spans, ignore_revs=self.ignore)
        if {owner.line for owner in plain} != lines:
            raise BlameRefused(f"{path}: git blamed other lines than the {len(lines)} asked for")
        ranges = owner_ranges(plain, filtered, self.mechanical)
        self.conn.execute(_DELETE_FILE_RANGES, (file_id, self.pinned))
        rows = [
            (
                self.repo_id,
                file_id,
                self.pinned,
                f"[{found.start},{found.stop})",
                found.commit,
                found.path,
                found.orig_line,
                found.effective,
                self.made_by,
            )
            for found in ranges
        ]
        with self.conn.cursor() as cur:
            cur.executemany(_INSERT_RANGE, rows)
        self.counts["files_blamed"] += 1
        self.counts["blame_rows"] += len(ranges)


def _refuse_other_commands(
    conn: Any, repo_id: int, pinned: str, made_by: str, logged_by: str
) -> None:
    for table, current in ((BLAME_RANGES, made_by), (LINE_LOG_COMMITS, logged_by)):
        others = sorted(str(a) for (a,) in conn.execute(_ALGOS[table], (repo_id, pinned)))
        others = [algo for algo in others if algo != current]
        if others:
            raise BlameRefused(
                f"{table} holds rows another command made ({others[0]}); rerun with --rebuild"
            )


def build(
    conn: Any,
    *,
    repo: Path,
    repo_id: int,
    target: Target,
    rebuild: bool = False,
    progress: Callable[[str], None] | None = None,
) -> BuildResult:
    """Blame each target file twice and log each target function, one file per transaction.

    Resumes: a file whose needed lines all have rows, and a function with logged rows, are
    skipped. Refuses, before writing anything, a shallow clone, a pinned commit outside the
    graph, an empty mechanical list, and rows another command made (unless `rebuild`).
    """
    _assert_not_shallow(repo)
    pinned = target.pinned
    if conn.execute(_COMMIT_KNOWN, (repo_id, pinned)).fetchone() is None:
        raise BlameRefused(f"pinned commit {pinned[:10]} is not in the graph: run `pace ingest`")
    mechanical = [str(sha) for (sha,) in conn.execute(_MECHANICAL, (repo_id,))]
    if not mechanical:
        raise BlameRefused("no commit is marked mechanical: filtered blame would be plain blame")
    by_path: dict[str, list[str]] = defaultdict(list)
    for path, name in target.functions:
        by_path[path].append(name)
    ids = file_ids(conn, repo_id, sorted(by_path))
    version = git_version()
    with tempfile.TemporaryDirectory(prefix="pace-blame-") as tmp:
        ignore = Path(tmp) / "mechanical-revs"
        ignore.write_text("".join(f"{sha}\n" for sha in mechanical), encoding="ascii")
        digest = hashlib.sha256(ignore.read_bytes()).hexdigest()
        made_by = blame_algo(version, len(mechanical), digest)
        logged_by = log_algo(version)
        if rebuild:
            for table in (BLAME_RANGES, LINE_LOG_COMMITS):
                conn.execute(_DELETE_SNAPSHOT.format(table), (repo_id, pinned))
            conn.commit()
        _refuse_other_commands(conn, repo_id, pinned, made_by, logged_by)
        run = _Run(conn, repo, repo_id, pinned, ignore, frozenset(mechanical), made_by, logged_by)
        for done, path in enumerate(sorted(by_path), start=1):
            try:
                run.file(path, ids[path], sorted(by_path[path]))
            except Exception:
                conn.rollback()
                raise
            conn.commit()
            if progress is not None and (done % 10 == 0 or done == len(by_path)):
                progress(f"{done}/{len(by_path)} files: {dict(sorted(run.counts.items()))}")
    counts = run.counts
    return BuildResult(
        files_blamed=counts["files_blamed"],
        files_skipped=counts["files_skipped"],
        blame_rows=counts["blame_rows"],
        functions_logged=counts["functions_logged"],
        functions_skipped=counts["functions_skipped"],
        log_rows=counts["log_rows"],
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`pace blame build`: arms A and L for the retained dev questions' functions."""
    from provenance.eval.runner import FULL_GOLDEN
    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    parser = argparse.ArgumentParser(
        prog="pace blame build",
        description=(
            "Blame the retained dev questions' functions twice, with and without mechanical "
            "commits, and store what `git log -L` lists for each (slice C2)."
        ),
    )
    parser.add_argument("--golden", default=str(FULL_GOLDEN), help=f"default: {FULL_GOLDEN}")
    parser.add_argument("--corpus", default=None, help="corpus path (default: settings)")
    parser.add_argument("--rebuild", action="store_true", help="delete this snapshot's rows first")
    args = parser.parse_args(list(argv) if argv is not None else None)

    corpus = Path(args.corpus) if args.corpus else Path(settings.corpus_path)
    try:
        target = golden_target(Path(args.golden))
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            result = build(
                conn,
                repo=corpus,
                repo_id=repo_id,
                target=target,
                rebuild=args.rebuild,
                progress=print,
            )
    except (UnknownRepository, RuntimeError) as exc:  # BlameRefused, GitFailed, a shallow clone
        print(f"BLAME REFUSED: {exc}", file=sys.stderr)
        return 1
    print(result.summary())
    return 0
