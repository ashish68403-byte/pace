"""The real history walk over apache/airflow.

Every rule in this module is here because breaking it produced a measured wrong
number on this corpus.

1. NEVER path-scope the walk.
   Commit ``2025-03-21`` moved ``airflow/`` -> ``airflow-core/src/airflow/``.
   ``git log -- airflow-core/src/airflow`` reports **4,007** commits; the full
   walk reports **17,015**. A path-scoped walk therefore drops ~76% of the
   rationale, and it drops it *silently* — the command succeeds. So: walk the
   whole history, detect renames ourselves, and reconstruct the rename chains.
   ``git log --follow`` does handle this, but it accepts exactly one pathspec, so
   605 files means 605 full-history walks. That does not scale on 2 cores.

2. NEVER shallow-clone.
   The repository has a graft/shallow boundary; blame and the walk stop dead
   there, producing plausible-looking but truncated authorship.

3. git's own rename detection degrades exactly where it matters.
   ``diff.renameLimit`` defaults to **1000** paths. The Black reformat commit
   touches 1,066 files and the PEP-563 ``from __future__ import annotations``
   commit touches 1,090 — both ABOVE the limit, so git quietly gives up on
   rename detection for precisely the commits with the most churn. We raise the
   limit explicitly for the subprocess path and set the pygit2 rename-detection
   flags ourselves.

4. One long-lived ``git cat-file --batch`` per worker.
   ``git show`` per blob forks a process per blob; at ~10^6 blob reads that is
   the entire runtime. The batch process is fed object ids parsed from
   ``git log --raw``.

Durability: a checkpoint row every ``checkpoint_every`` files, per-document
isolation (one bad file becomes a ``dead_letters`` row, never a lost run), and a
failure count in the returned :class:`WalkResult`.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from provenance.config import settings
from provenance.graph.tables import REPOSITORIES

log = logging.getLogger(__name__)

__all__ = [
    "CatFileBatch",
    "CheckpointSchemaError",
    "Checkpointer",
    "ConcurrentRunError",
    "CommitRecord",
    "FileChange",
    "RenameChains",
    "WalkResult",
    "ensure_repository",
    "is_mechanical",
    "load_blame_ignore_revs",
    "main",
    "raw_log_stream",
    "rev_parse",
    "walk_history",
]

# --------------------------------------------------------------------- mechanical

# Subjects that mean "this commit changed bytes, not meaning". A mechanical commit
# must never be cited as the rationale for a line: it is why blame points at the
# wrong author, and why "why is this line here?" answers with "reformat".
MECHANICAL_SUBJECT_RE = re.compile(
    r"""(?ix)
    ^(?:\[?(?:pre-commit(?:\.ci)?|dependabot|renovate)]?\s*)?
    (?:.*\b(?:
        pre-?commit
      | ruff
      | black
      | isort
      | flake8
      | pyupgrade
      | autoflake
      | bump\ (?:version|to|up)
      | bump[-_]?(?:version|deps|dependencies)
      | re-?format(?:ting|ted)?
      | reformat
      | formatting
      | code\ style
      | typing\ (?:fixes|improvements)?
      | add\ type\ (?:hints|annotations)
      | mypy
      | translations?
      | i18n
      | update\ (?:pre-commit|licence|license)
    )\b)
    """,
)

BOT_AUTHOR_RE = re.compile(
    r"(?i)(?:\[bot\]|^dependabot|^renovate|^pre-commit(?:\.ci)?|^github-actions|"
    r"^apache-?airflow-?bot|noreply@github\.com$)"
)

BLAME_IGNORE_FILE = ".git-blame-ignore-revs"


@dataclass(slots=True)
class FileChange:
    """One path touched by one commit, with the rename resolved."""

    status: str  # A M D R C T
    old_path: str | None
    new_path: str | None
    old_blob: str | None
    new_blob: str | None
    similarity: int | None = None  # R100 -> 100

    @property
    def is_rename(self) -> bool:
        return self.status.startswith("R")


@dataclass(slots=True)
class CommitRecord:
    sha: str
    parents: list[str]
    author_name: str
    author_email: str
    author_time: datetime
    committer_email: str
    subject: str
    message: str
    changes: list[FileChange] = field(default_factory=list)
    mechanical: bool = False
    mechanical_reason: str | None = None


@dataclass(slots=True)
class WalkResult:
    #: ingest_runs.run_id, a bigint assigned by the database (the column is
    #: GENERATED ALWAYS AS IDENTITY -- a client-side UUID cannot be written to
    #: it). None means the run was never opened and progress went to the local
    #: spill file instead.
    run_id: int | None
    commits: int = 0
    files_seen: int = 0
    renames: int = 0
    mechanical: int = 0
    failures: int = 0
    blame_ignore_revs: int = 0
    #: True when the run could not be persisted and went to the spill file.
    degraded: bool = False

    def summary(self) -> str:
        return (
            f"run {self.run_id if self.run_id is not None else 'unpersisted'}: "
            f"{self.commits:,} commits, {self.files_seen:,} file-touches, "
            f"{self.renames:,} renames, {self.mechanical:,} mechanical, "
            f"{self.failures:,} failures (dead-lettered), "
            f"{self.blame_ignore_revs} blame-ignore revs"
        )


# ------------------------------------------------------------------ blame-ignore


def load_blame_ignore_revs(repo_path: Path | None = None) -> set[str]:
    """Full 40-hex SHAs listed in ``.git-blame-ignore-revs`` at the repo root.

    Airflow ships 7 of these. Two of them are the reason this file exists at all:
    a Black reformat touching 1,066 files and a PEP-563 annotations commit
    touching 1,090. Without marking them, blame attributes ~2,000 files to two
    commits that explain nothing.
    """
    root = Path(repo_path or settings.corpus_path)
    ignore_file = root / BLAME_IGNORE_FILE
    if not ignore_file.exists():
        log.warning("%s not found; mechanical detection loses its strongest signal", ignore_file)
        return set()

    revs: set[str] = set()
    for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
        token = line.split("#", 1)[0].strip()
        if not token:
            continue
        if re.fullmatch(r"[0-9a-f]{40}", token):
            revs.add(token)
            continue
        # Abbreviated or symbolic: resolve through git so the set is uniformly full SHAs.
        resolved = rev_parse(root, token, peel_to_commit=True)
        if resolved:
            revs.add(resolved)
    return revs


def rev_parse(root: Path, rev: str, *, peel_to_commit: bool = False) -> str | None:
    """Resolve ``rev`` to a full object id, or None if git cannot.

    Public because the walking skeleton reads its one blob through exactly the
    same helper the full walk uses -- a second copy is a second set of git flags
    to keep in sync.

    ``peel_to_commit`` appends ``^{commit}``, which is required for a tag in
    .git-blame-ignore-revs and WRONG for a ``<sha>:<path>`` blob lookup (a blob
    cannot be peeled to a commit, so git errors and this returns None).
    """
    spec = f"{rev}^{{commit}}" if peel_to_commit else rev
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", spec],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        log.warning("could not resolve rev %r in %s", rev, root)
        return None
    return out.stdout.strip() or None


def is_mechanical(
    subject: str,
    author_name: str,
    author_email: str,
    *,
    sha: str = "",
    blame_ignore: Sequence[str] | set[str] = (),
    whitespace_only: bool | None = None,
) -> tuple[bool, str | None]:
    """Return ``(mechanical, reason)``.

    Four independent signals; any one is sufficient. They are checked cheapest
    first — the whitespace-only diff is the expensive one and is passed in
    pre-computed by the caller.
    """
    if sha and sha in blame_ignore:
        return True, "blame-ignore-revs"
    if BOT_AUTHOR_RE.search(author_email) or BOT_AUTHOR_RE.search(author_name):
        return True, "bot-author"
    if MECHANICAL_SUBJECT_RE.search(subject):
        return True, "subject-regex"
    if whitespace_only:
        return True, "whitespace-only-diff"
    return False, None


# ------------------------------------------------------------------ cat-file batch


class CatFileBatch(AbstractContextManager["CatFileBatch"]):
    """One long-lived ``git cat-file --batch`` process. ONE per worker, reused.

    ``git show <oid>`` costs a fork+exec per blob. Over a full Airflow walk that is
    on the order of a million forks and dominates wall-clock on 2 cores. This keeps
    a single process alive and streams ``<oid>\\n`` requests into it.
    """

    def __init__(self, repo_path: Path | None = None) -> None:
        self.repo_path = Path(repo_path or settings.corpus_path)
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> CatFileBatch:
        self._proc = subprocess.Popen(
            ["git", "-C", str(self.repo_path), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover - best effort teardown
            proc.kill()

    def read(self, oid: str) -> bytes | None:
        """Return blob bytes, or ``None`` for a missing object (e.g. the all-zero oid)."""
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("CatFileBatch used outside its context manager")
        if not oid or set(oid) == {"0"}:
            return None

        self._proc.stdin.write(oid.encode("ascii") + b"\n")
        self._proc.stdin.flush()
        header = self._proc.stdout.readline().decode("utf-8", errors="replace").strip()
        if not header or header.endswith(("missing", "ambiguous")):
            return None
        try:
            _oid, _type, size_s = header.split()
            size = int(size_s)
        except ValueError:  # pragma: no cover - corrupt stream
            raise RuntimeError(f"unexpected cat-file header: {header!r}") from None

        buf = bytearray()
        while len(buf) < size:
            block = self._proc.stdout.read(size - len(buf))
            if not block:
                raise RuntimeError("cat-file --batch stream closed mid-object")
            buf.extend(block)
        self._proc.stdout.read(1)  # trailing newline
        return bytes(buf)


# -------------------------------------------------------------------- rename chains


class RenameChains:
    """Maps a historical path to the path it has *today*.

    Built while walking newest -> oldest: when commit C renames ``old -> new``,
    every appearance of ``old`` in commits older than C is the same file as
    ``new``. Because we already know ``new``'s canonical name (we saw it more
    recently), one dict lookup resolves the whole chain — no per-file
    ``--follow`` walk, no O(files x history).
    """

    __slots__ = ("_alias", "_renames")

    def __init__(self) -> None:
        self._alias: dict[str, str] = {}
        self._renames = 0

    @property
    def rename_count(self) -> int:
        return self._renames

    def record(self, old_path: str, new_path: str) -> None:
        canonical = self.resolve(new_path)
        if old_path != canonical:
            self._alias[old_path] = canonical
            self._renames += 1

    def resolve(self, path: str) -> str:
        seen: set[str] = set()
        current = path
        while current in self._alias:
            if current in seen:  # pathological cycle from a rename/rename conflict
                break
            seen.add(current)
            current = self._alias[current]
        return current

    def as_dict(self) -> dict[str, str]:
        return dict(self._alias)


# --------------------------------------------------------------- raw log streaming


def raw_log_stream(
    repo_path: Path | None = None,
    *,
    rev: str = "HEAD",
    rename_threshold: int = 50,
) -> Iterator[CommitRecord]:
    """Stream the FULL history with rename detection, one :class:`CommitRecord` at a time.

    Note the explicit ``-c diff.renameLimit=0`` (0 == unlimited): the default is
    1000, which is *below* the 1,066-file Black commit and the 1,090-file PEP-563
    commit, so git would otherwise disable rename detection on exactly the commits
    where paths move the most.

    Deliberately NO pathspec (see module docstring) and NO ``--depth``.
    """
    root = Path(repo_path or settings.corpus_path)
    sep = "\x1e"  # record separator; safe against any subject text
    fmt = sep + "%H%x00%P%x00%an%x00%ae%x00%aI%x00%ce%x00%s%x00%B%x00"
    cmd = [
        "git",
        "-C",
        str(root),
        "-c",
        "diff.renameLimit=0",
        "-c",
        "diff.renames=copies",
        "log",
        rev,
        "--raw",
        "--no-abbrev",
        f"--find-renames={rename_threshold}%",
        "--no-textconv",
        "-m",  # show diffs for merge commits' first parent too
        "--first-parent",
        f"--format={fmt}",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert proc.stdout is not None
    try:
        buffer: list[str] = []
        for line in proc.stdout:
            if line.startswith(sep):
                if buffer:
                    record = _parse_raw_record("".join(buffer))
                    if record is not None:
                        yield record
                buffer = [line]
            else:
                buffer.append(line)
        if buffer:
            record = _parse_raw_record("".join(buffer))
            if record is not None:
                yield record
    finally:
        proc.stdout.close()
        proc.wait()


_RAW_RE = re.compile(
    r"^:+(?P<modes>[0-7 ]+)\s(?P<blobs>[0-9a-f ]+)\s(?P<status>[A-Z]\d*)\t(?P<paths>.*)$"
)


def _parse_raw_record(block: str) -> CommitRecord | None:
    body = block.lstrip("\x1e")
    fields = body.split("\x00")
    if len(fields) < 8:
        return None
    sha, parents, an, ae, aiso, ce, subject, message = fields[:8]
    tail = fields[8] if len(fields) > 8 else ""

    changes: list[FileChange] = []
    for line in tail.splitlines():
        if not line.startswith(":"):
            continue
        m = _RAW_RE.match(line)
        if not m:
            continue
        blobs = m.group("blobs").split()
        status = m.group("status")
        paths = m.group("paths").split("\t")
        old_blob = blobs[-2] if len(blobs) >= 2 else None
        new_blob = blobs[-1] if blobs else None
        if status[0] in {"R", "C"} and len(paths) == 2:
            old_path, new_path = paths
        elif status[0] == "D":
            old_path, new_path = paths[0], None
        elif status[0] == "A":
            old_path, new_path = None, paths[0]
        else:
            old_path = new_path = paths[0]
        similarity = int(status[1:]) if len(status) > 1 and status[1:].isdigit() else None
        changes.append(
            FileChange(
                status=status,
                old_path=old_path,
                new_path=new_path,
                old_blob=old_blob,
                new_blob=new_blob,
                similarity=similarity,
            )
        )

    return CommitRecord(
        sha=sha,
        parents=parents.split() if parents else [],
        author_name=an,
        author_email=ae,
        author_time=_parse_iso(aiso),
        committer_email=ce,
        subject=subject,
        message=message,
        changes=changes,
    )


def _parse_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:  # pragma: no cover - defensive
        return datetime.fromtimestamp(0, tz=UTC)


# ---------------------------------------------------------------- pygit2 whitespace


def _whitespace_only(repo: Any, commit: Any) -> bool:
    """True if the commit's diff vanishes once whitespace is ignored.

    pygit2 is used here rather than a second subprocess because we need the diff
    twice (with and without whitespace) and the trees are already in cache.
    """
    import pygit2

    if not commit.parents:
        return False
    try:
        plain = repo.diff(commit.parents[0].tree, commit.tree, context_lines=0)
        ignored = repo.diff(
            commit.parents[0].tree,
            commit.tree,
            context_lines=0,
            flags=pygit2.GIT_DIFF_IGNORE_WHITESPACE,
        )
    except Exception:  # pragma: no cover - broken tree / submodule
        return False
    plain_changed = plain.stats.insertions + plain.stats.deletions
    ignored_changed = ignored.stats.insertions + ignored.stats.deletions
    return plain_changed > 0 and ignored_changed == 0


# ------------------------------------------------------------------- checkpointing

# Physical names. provenance.graph.tables is the single source of truth for the
# query layer; it does not (yet) carry the ingest-bookkeeping tables, so they are
# defined here exactly once and should move there the moment a second subtree
# needs them. Both are PLURAL and neither has the columns the first draft of this
# class invented (`kind`, `files_done`, `failures`, `updated_at`, `ref`).
INGEST_RUNS: Final = "ingest_runs"
DEAD_LETTERS: Final = "dead_letters"

# ingest_status enum (0001_core): pending | running | succeeded | failed |
# cancelled. 'ok' and 'completed' are NOT members -- passing either is
# `invalid input value for enum ingest_status`. The CHECK constraint
# ingest_runs_finished_iff_terminal additionally requires finished_at to be set
# exactly when the status is terminal.
STATUS_RUNNING: Final = "running"
STATUS_SUCCEEDED: Final = "succeeded"
STATUS_FAILED: Final = "failed"
STATUS_CANCELLED: Final = "cancelled"
TERMINAL_STATUSES: Final = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED})

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# The repositories row must exist before ingest_runs.repo_id (NOT NULL, FK) can
# be written. Reading is provenance.graph.repos' job; only the write lives here.
_INSERT_REPOSITORY = f"""
    INSERT INTO {REPOSITORIES} (owner, name, clone_path)
    VALUES (%(owner)s, %(name)s, %(clone_path)s)
    ON CONFLICT (owner, name)
    DO UPDATE SET clone_path = coalesce(EXCLUDED.clone_path, {REPOSITORIES}.clone_path)
    RETURNING repo_id
"""


class CheckpointSchemaError(RuntimeError):
    """The checkpoint SQL does not match the database.

    Deliberately fatal, and deliberately NOT degraded to the spill file. A spill
    is the right answer to an outage -- the database was there and went away, and
    a three-hour walk should survive it. It is the wrong answer to a query that is
    simply wrong, because that condition never clears: the run would spill for
    three hours, log one warning at minute zero, and finish reporting success
    while ingest_runs stayed empty. That is precisely what this class used to do.
    """


class ConcurrentRunError(RuntimeError):
    """Another run is already `running` for this (repo_id, driver).

    ingest_runs_one_running_per_driver is a PARTIAL unique index -- finished runs
    may pile up, live ones may not -- so this is a real, actionable conflict and
    not a schema bug. Usually it means a previous walk was killed without closing
    its row: mark it cancelled, or resume from its checkpoint.
    """


def ensure_repository(
    conn: Any,
    *,
    owner: str | None = None,
    name: str | None = None,
    clone_path: Path | str | None = None,
) -> int:
    """Return the bigint repo_id for ``owner/name``, creating the row if absent.

    Reads through provenance.graph.repos so there is exactly one resolver; only
    the INSERT lives here, because the walker is the thing that first learns a
    repository exists. repo_id is a bigint -- never the "owner/name" slug.
    """
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    owner = owner or settings.repo_owner
    name = name or settings.repo_name
    try:
        return resolve_repo_id(conn, owner, name)
    except UnknownRepository:
        pass
    row = conn.execute(
        _INSERT_REPOSITORY,
        {"owner": owner, "name": name, "clone_path": str(clone_path) if clone_path else None},
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields a row here
        raise CheckpointSchemaError(f"could not create repositories row for {owner}/{name}")
    return int(row[0])


class Checkpointer:
    """Persist walk progress into ``ingest_runs`` every N files.

    A full walk is hours on this hardware; dying at hour three with nothing
    written is the failure this prevents.

    WHAT BROKE HERE BEFORE, because it will break again the same way: the SQL was
    written against a remembered schema instead of the migration. It inserted
    `kind`, `files_done`, `failures` and `updated_at` (the real columns are
    `driver`, `rows_*` and `heartbeat_at`), handed a UUID string to a `run_id`
    that is GENERATED ALWAYS AS IDENTITY, never supplied the NOT NULL `repo_id`,
    and wrote `dead_letters.ref` where the column is `subject_ref`. Every
    statement failed -- and the old `_execute` caught everything, so a multi-hour
    walk quietly spilled to a JSON file while appearing to checkpoint to Postgres.

    Two consequences, both deliberate:
      * per-file progress that has no column lives in the `checkpoint` jsonb,
        which is exactly what 0001_core says that column is for;
      * a SQL/schema mismatch raises (:class:`CheckpointSchemaError`) and only a
        genuine outage degrades to the spill file.
    """

    INSERT_RUN = f"""
        INSERT INTO {INGEST_RUNS} (repo_id, driver, driver_version, status, checkpoint, trace_id)
        VALUES (%(repo_id)s, %(driver)s, %(driver_version)s, %(status)s::ingest_status,
                %(checkpoint)s::jsonb, %(trace_id)s)
        RETURNING run_id
    """
    UPDATE_CHECKPOINT = f"""
        UPDATE {INGEST_RUNS}
           SET checkpoint    = %(checkpoint)s::jsonb,
               rows_inserted = %(rows_inserted)s,
               rows_updated  = %(rows_updated)s,
               rows_deleted  = %(rows_deleted)s,
               rows_skipped  = %(rows_skipped)s,
               heartbeat_at  = now()
         WHERE run_id = %(run_id)s
    """
    # finished_at and a terminal status must move together or the CHECK
    # constraint ingest_runs_finished_iff_terminal rejects the row.
    FINISH_RUN = f"""
        UPDATE {INGEST_RUNS}
           SET status           = %(status)s::ingest_status,
               checkpoint       = %(checkpoint)s::jsonb,
               rows_inserted    = %(rows_inserted)s,
               rows_updated     = %(rows_updated)s,
               rows_deleted     = %(rows_deleted)s,
               rows_skipped     = %(rows_skipped)s,
               last_indexed_sha = %(last_indexed_sha)s,
               error_class      = %(error_class)s,
               error_message    = %(error_message)s,
               heartbeat_at     = now(),
               finished_at      = now()
         WHERE run_id = %(run_id)s
    """
    # One row per failing subject, not one per retry: dead_letters_subject_key is
    # a unique index over (repo_id, stage, subject_ref, coalesce(commit_sha::text,
    # '')), and ON CONFLICT has to name that expression exactly to match it.
    INSERT_DEAD_LETTER = f"""
        INSERT INTO {DEAD_LETTERS}
            (repo_id, run_id, stage, subject_ref, commit_sha,
             error_class, error_message, payload)
        VALUES (%(repo_id)s, %(run_id)s, %(stage)s, %(subject_ref)s, %(commit_sha)s,
                %(error_class)s, %(error_message)s, %(payload)s::jsonb)
        ON CONFLICT (repo_id, stage, subject_ref, (coalesce(commit_sha::text, '')))
        DO UPDATE SET retry_count   = {DEAD_LETTERS}.retry_count + 1,
                      last_seen_at  = now(),
                      error_class   = EXCLUDED.error_class,
                      error_message = EXCLUDED.error_message,
                      payload       = EXCLUDED.payload
    """

    def __init__(
        self,
        *,
        repo_id: int | None = None,
        every: int = 250,
        driver: str = "git_walk",
        driver_version: str = "v1",
        trace_id: str | None = None,
        clone_path: Path | str | None = None,
        allow_spill: bool = True,
    ) -> None:
        self.every = every
        self.driver = driver
        self.driver_version = driver_version
        self.trace_id = trace_id
        self.clone_path = clone_path
        self.allow_spill = allow_spill
        self.repo_id = repo_id
        self.run_id: int | None = None
        self.files_done = 0
        self.failures = 0
        self.rows: dict[str, int] = {"inserted": 0, "updated": 0, "deleted": 0, "skipped": 0}
        # Names the spill file even when the database never allocated a run_id.
        self.local_id = uuid.uuid4().hex
        self._since_flush = 0
        self._degraded = False
        self._state: dict[str, Any] = {}
        self._start()

    # -- plumbing ---------------------------------------------------------

    @property
    def degraded(self) -> bool:
        """True when progress is going to a local file instead of Postgres."""
        return self._degraded

    @staticmethod
    def _outage_errors() -> tuple[type[BaseException], ...]:
        """Exception types that mean "the database is not reachable right now".

        psycopg_pool.PoolError (and therefore PoolTimeout) subclasses
        psycopg.OperationalError, so the pool's own failures are covered here.
        Everything else psycopg raises -- UndefinedTable, UndefinedColumn,
        InvalidTextRepresentation, NotNullViolation, an invalid enum label -- is a
        bug in this file's SQL and must not be survivable.
        """
        import psycopg

        return (psycopg.OperationalError, psycopg.InterfaceError)

    def _run(self, work: Callable[[Any], Any], *, what: str) -> Any:
        """Run ``work(conn)`` on a pooled connection, classifying any failure."""
        if self._degraded:
            self._spill()
            return None
        try:
            import psycopg

            from provenance.graph.db import connection
        except ImportError as exc:  # no driver installed at all
            self._degrade(what, exc)
            return None

        try:
            with connection() as conn:
                result = work(conn)
                conn.commit()
                return result
        except self._outage_errors() as exc:
            self._degrade(what, exc)
            return None
        except psycopg.Error as exc:
            # LOUD. A spill file here would hide a permanent condition behind a
            # single warning for the whole run.
            raise CheckpointSchemaError(
                f"{what} failed against the live schema: {type(exc).__name__}: {exc}. "
                "This is a mismatch between this module's SQL and the migrations, "
                "not an outage, so it is NOT being spilled to a file. Fix the SQL "
                "against migrations/versions/0001_core.py."
            ) from exc

    def _degrade(self, what: str, exc: BaseException) -> None:
        if not self.allow_spill:
            raise CheckpointSchemaError(
                f"{what} could not reach the database ({exc}) and spilling is disabled"
            )
        self._degraded = True
        log.warning(
            "database unreachable during %s (%s); checkpointing to %s for the rest of this run",
            what,
            exc,
            self._spill_path(),
        )
        self._spill()

    def _spill_path(self) -> Path:
        name = str(self.run_id) if self.run_id is not None else f"local-{self.local_id}"
        return Path.home() / ".pace" / "checkpoints" / f"{name}.json"

    def _spill(self) -> None:
        path = self._spill_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "repo_id": self.repo_id,
                    "driver": self.driver,
                    "rows": self.rows,
                    "checkpoint": self._checkpoint(),
                    "heartbeat_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    def _checkpoint(self) -> dict[str, Any]:
        """The resume cursor. files_done/failures live HERE, not in columns.

        ingest_runs has no files_done and no failures column; `checkpoint jsonb`
        is the migration's documented home for whatever a driver needs to resume,
        precisely so adding a counter is not a migration.
        """
        return {
            "driver": self.driver,
            "files_done": self.files_done,
            "failures": self.failures,
            **self._state,
        }

    def _start(self) -> None:
        def work(conn: Any) -> Any:
            if self.repo_id is None:
                self.repo_id = ensure_repository(conn, clone_path=self.clone_path)
            return conn.execute(
                self.INSERT_RUN,
                {
                    "repo_id": self.repo_id,
                    "driver": self.driver,
                    "driver_version": self.driver_version,
                    "status": STATUS_RUNNING,
                    "checkpoint": json.dumps(self._checkpoint(), default=str),
                    "trace_id": self.trace_id,
                },
            ).fetchone()

        try:
            row = self._run(work, what="opening the ingest run")
        except CheckpointSchemaError as exc:
            if "ingest_runs_one_running_per_driver" in str(exc):
                raise ConcurrentRunError(
                    f"a {self.driver!r} run is already marked 'running' for repo_id "
                    f"{self.repo_id}. Close it first: UPDATE ingest_runs SET status = "
                    f"'{STATUS_CANCELLED}', finished_at = now() WHERE status = 'running' "
                    f"AND driver = '{self.driver}';"
                ) from exc
            raise
        if row is not None:
            self.run_id = int(row[0])
            log.info(
                "ingest run %s (repo_id=%s, driver=%s)", self.run_id, self.repo_id, self.driver
            )

    # -- public API -------------------------------------------------------

    def record_rows(
        self, *, inserted: int = 0, updated: int = 0, deleted: int = 0, skipped: int = 0
    ) -> None:
        """Accumulate the counters the idempotency gate reads.

        Re-running a driver over an unchanged corpus must produce
        inserted = updated = deleted = 0. That assertion is a CI gate, so these
        have to be real counts written to the real columns.
        """
        self.rows["inserted"] += inserted
        self.rows["updated"] += updated
        self.rows["deleted"] += deleted
        self.rows["skipped"] += skipped

    def advance(self, **state: Any) -> None:
        """Record progress for one file/commit; flushes every ``every`` calls."""
        self.files_done += 1
        self._since_flush += 1
        self._state.update(state)
        if self._since_flush >= self.every:
            self.flush()

    def flush(self) -> None:
        self._since_flush = 0
        if self.run_id is None:
            self._spill()
            return
        self._run(
            lambda conn: conn.execute(
                self.UPDATE_CHECKPOINT,
                {
                    "run_id": self.run_id,
                    "checkpoint": json.dumps(self._checkpoint(), default=str),
                    "rows_inserted": self.rows["inserted"],
                    "rows_updated": self.rows["updated"],
                    "rows_deleted": self.rows["deleted"],
                    "rows_skipped": self.rows["skipped"],
                },
            ),
            what="writing a checkpoint",
        )

    def dead_letter(
        self,
        stage: str,
        subject_ref: str,
        exc: BaseException,
        *,
        commit_sha: str | None = None,
        **payload: Any,
    ) -> None:
        """Per-document isolation: one unparseable file must not abort 17,015 commits."""
        self.failures += 1
        log.warning("dead-letter [%s] %s: %s: %s", stage, subject_ref, type(exc).__name__, exc)
        sha = commit_sha or (subject_ref if _FULL_SHA_RE.match(subject_ref) else None)
        if self.repo_id is None:
            # dead_letters.repo_id is NOT NULL; with no repository row there is
            # nothing to attach this to, so the spill file is the honest record.
            self._spill()
            return
        self._run(
            lambda conn: conn.execute(
                self.INSERT_DEAD_LETTER,
                {
                    "repo_id": self.repo_id,
                    "run_id": self.run_id,
                    "stage": stage,
                    "subject_ref": subject_ref,
                    # git_sha is a domain with a 40-lower-hex CHECK: anything
                    # else has to go in as NULL or the insert is rejected.
                    "commit_sha": sha,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc)[:4000],
                    "payload": json.dumps(payload, default=str),
                },
            ),
            what="writing a dead letter",
        )

    def finish(
        self,
        status: str = STATUS_SUCCEEDED,
        *,
        error: BaseException | None = None,
        last_indexed_sha: str | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(
                f"{status!r} is not a terminal ingest_status "
                f"({sorted(TERMINAL_STATUSES)}). 'completed' and 'ok' are not enum "
                "members, and finished_at may only be set alongside a terminal status."
            )
        self._since_flush = 0
        if self.run_id is None:
            self._spill()
            return
        sha = (
            last_indexed_sha if last_indexed_sha and _FULL_SHA_RE.match(last_indexed_sha) else None
        )
        self._run(
            lambda conn: conn.execute(
                self.FINISH_RUN,
                {
                    "run_id": self.run_id,
                    "status": status,
                    "checkpoint": json.dumps(self._checkpoint(), default=str),
                    "rows_inserted": self.rows["inserted"],
                    "rows_updated": self.rows["updated"],
                    "rows_deleted": self.rows["deleted"],
                    "rows_skipped": self.rows["skipped"],
                    "last_indexed_sha": sha,
                    "error_class": type(error).__name__ if error is not None else None,
                    "error_message": str(error)[:4000] if error is not None else None,
                },
            ),
            what="closing the ingest run",
        )


# ------------------------------------------------------------------------ the walk


def walk_history(
    repo_path: Path | None = None,
    *,
    rev: str = "HEAD",
    scope: Sequence[str] | None = None,
    checkpoint_every: int = 250,
    on_commit: Any = None,
    max_commits: int | None = None,
    repo_id: int | None = None,
    trace_id: str | None = None,
) -> tuple[WalkResult, RenameChains]:
    """Walk the full history newest -> oldest and yield rename-resolved commits.

    ``scope`` filters which *present-day* paths we care about — applied AFTER the
    rename chain resolves a historical path to its modern name, which is the whole
    point: ``airflow/models/dag.py`` in 2016 must still land in the scope entry
    ``airflow-core/src/airflow/models/dag.py``.

    ``on_commit(record, resolved_paths)`` is called per commit; any exception it
    raises is dead-lettered and the walk continues.
    """
    root = Path(repo_path or settings.corpus_path)
    _assert_not_shallow(root)

    scope_set = set(scope) if scope is not None else None
    blame_ignore = load_blame_ignore_revs(root)
    chains = RenameChains()
    checkpointer = Checkpointer(
        repo_id=repo_id,
        every=checkpoint_every,
        clone_path=root,
        trace_id=trace_id,
    )
    result = WalkResult(run_id=checkpointer.run_id, blame_ignore_revs=len(blame_ignore))
    last_sha: str | None = None

    repo = _open_repo(root)
    try:
        for record in raw_log_stream(root, rev=rev):
            if max_commits is not None and result.commits >= max_commits:
                break
            result.commits += 1

            # Renames must be recorded before resolving this commit's own paths:
            # a rename commit's *old* path is what older commits will ask about.
            resolved: list[tuple[FileChange, str]] = []
            for change in record.changes:
                result.files_seen += 1
                if change.is_rename and change.old_path and change.new_path:
                    chains.record(change.old_path, change.new_path)
                probe = change.new_path or change.old_path or ""
                canonical = chains.resolve(probe)
                if scope_set is None or canonical in scope_set:
                    resolved.append((change, canonical))

            # Cheap signals first; only pay for the double diff when they all miss.
            record.mechanical, record.mechanical_reason = is_mechanical(
                record.subject,
                record.author_name,
                record.author_email,
                sha=record.sha,
                blame_ignore=blame_ignore,
            )
            if not record.mechanical and repo is not None:
                try:
                    if _whitespace_only(repo, repo[record.sha]):
                        record.mechanical, record.mechanical_reason = True, "whitespace-only-diff"
                except Exception:  # noqa: BLE001 - a broken tree is not a walk-ending event
                    pass
            if record.mechanical:
                result.mechanical += 1

            if on_commit is not None and resolved:
                try:
                    on_commit(record, resolved)
                except Exception as exc:  # noqa: BLE001 - isolation is the point
                    checkpointer.dead_letter(
                        "git_walk", record.sha, exc, subject=record.subject[:200]
                    )

            last_sha = record.sha
            checkpointer.advance(last_sha=record.sha, last_time=record.author_time)

        result.renames = chains.rename_count
        result.failures = checkpointer.failures
        # 'succeeded', not 'completed': ingest_status has five members and
        # 'completed' is not one of them, so the old label was an enum error
        # (silently swallowed, so the run never closed).
        checkpointer.finish(STATUS_SUCCEEDED, last_indexed_sha=last_sha)
    except BaseException as exc:
        result.failures = checkpointer.failures
        checkpointer.finish(STATUS_FAILED, error=exc, last_indexed_sha=last_sha)
        raise
    result.degraded = checkpointer.degraded
    return result, chains


def _open_repo(root: Path) -> Any:
    try:
        import pygit2

        return pygit2.Repository(str(root))
    except Exception as exc:  # noqa: BLE001 - whitespace detection is optional, the walk is not
        log.warning("pygit2 unavailable (%s); whitespace-only detection disabled", exc)
        return None


def _assert_not_shallow(root: Path) -> None:
    """A shallow clone makes blame stop at the graft boundary and lie about it."""
    if (root / ".git" / "shallow").exists() or (root / "shallow").exists():
        raise RuntimeError(
            f"{root} is a shallow clone. Blame and the history walk stop dead at the graft "
            f"boundary and report truncated results without erroring. "
            f"Run: git -C {root} fetch --unshallow"
        )


# ------------------------------------------------------------------------- cli


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pace ingest",
        description="Walk the FULL git history of the corpus into the provenance graph.",
        epilog=(
            "The walk is never path-scoped and never shallow: the 2025-03-21 commit that "
            "moved airflow/ to airflow-core/src/airflow/ makes `git log -- <path>` report "
            "4,008 commits where the full walk reports 17,015, and it reports them without "
            "erroring. --scope filters PRESENT-DAY paths after rename resolution, which is a "
            "different thing entirely."
        ),
    )
    parser.add_argument("--repo", default=None, help="corpus path (default: settings.corpus_path)")
    parser.add_argument("--rev", default="HEAD", help="revision to walk back from")
    parser.add_argument(
        "--max-commits", type=int, default=None, help="stop after N commits (smoke runs)"
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=250,
        help="write ingest_runs.checkpoint every N commits",
    )
    parser.add_argument(
        "--scope",
        dest="scope_path",
        default=None,
        help="frozen scope file (default: the committed scope.txt; --no-scope walks everything)",
    )
    parser.add_argument(
        "--no-scope",
        action="store_true",
        help="do not filter by the frozen scope at all",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Thin CLI adapter for `pace ingest`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(list(argv) if argv is not None else None)

    scope: list[str] | None = None
    if not args.no_scope:
        from provenance.ingest.scope import load_scope

        try:
            scope = load_scope(Path(args.scope_path) if args.scope_path else None)
        except FileNotFoundError as exc:
            # Walking the whole tree silently would change what "the corpus" means
            # and therefore every recall number measured against it.
            print(f"{exc}\nRun `pace scope` first, or pass --no-scope deliberately.")
            return 2

    try:
        result, chains = walk_history(
            Path(args.repo) if args.repo else None,
            rev=args.rev,
            scope=scope,
            checkpoint_every=args.checkpoint_every,
            max_commits=args.max_commits,
        )
    except (CheckpointSchemaError, ConcurrentRunError) as exc:
        print(f"ingest aborted: {exc}")
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "commits": result.commits,
                    "files_seen": result.files_seen,
                    "renames": result.renames,
                    "mechanical": result.mechanical,
                    "failures": result.failures,
                    "blame_ignore_revs": result.blame_ignore_revs,
                    "degraded": result.degraded,
                    "scope_files": len(scope) if scope is not None else None,
                },
                indent=2,
            )
        )
    else:
        print(result.summary())
        print(f"rename chains: {len(chains.as_dict()):,} historical paths mapped to a modern name")
        if result.degraded:
            print("WARNING: progress was spilled to a local file; ingest_runs was NOT updated")
    return 0
