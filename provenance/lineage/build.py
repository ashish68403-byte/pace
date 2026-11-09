"""Airflow's function identities, built and stored (slice C3a).

docs/tasks/function-identity/002_lineage_engine.md: walk every in-scope file's history (checkpoint
A's `file_revisions`), run pass 1 per file and pass 2 across files, and write `symbol_lineage`,
`symbol_versions`, the ambiguous cases as `lineage_edges` and the refused revisions as
`dead_letters`, in one transaction and from scratch: the build is deterministic and takes minutes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any, Final

from provenance.config import settings
from provenance.graph.tables import (
    DEAD_LETTERS,
    FILE_REVISIONS,
    LINEAGE_EDGES,
    SYMBOL_LINEAGE,
    SYMBOL_VERSIONS,
)
from provenance.ingest.git_walk import CatFileBatch, _assert_not_shallow
from provenance.lineage.link import Linked, link_moves
from provenance.lineage.match import Ambiguity, FilePass, Revision, Symbol, walk_file
from provenance.parse.normalize import HashLadder, IdentityHashes, hash_ladder, identity_hashes
from provenance.parse.symbols import symbol_spans

DETECTOR: Final = "lineage-mvp-1"
#: `dead_letters.stage` for the revisions the chunker refused.
STAGE: Final = "lineage"

_REVISIONS = (
    "SELECT file_id, commit_sha, commit_topo_order, change_type::text, blob_sha, path_at_commit "
    f"FROM {FILE_REVISIONS} WHERE repo_id = %s ORDER BY file_id, commit_topo_order"
)
_LINEAGE = (
    f"INSERT INTO {SYMBOL_LINEAGE} (repo_id, symbol_kind, birth_commit, birth_topo_order, "
    "death_commit, death_topo_order, current_path, current_qualified_name, version_count, "
    "detector_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING lineage_id"
)
_VERSION = (
    f"INSERT INTO {SYMBOL_VERSIONS} (lineage_id, commit_topo_order, repo_id, commit_sha, path, "
    "qualified_name, start_line, end_line, h0, h1, h2, h3, ast_norm_version, match_stage, "
    "match_score, is_rename, is_move, is_body_change) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_EDGE = (
    f"INSERT INTO {LINEAGE_EDGES} (repo_id, kind, src_lineage_id, dst_lineage_id, at_commit, "
    "at_topo_order, confidence, detector, detector_version, evidence) "
    "VALUES (%s, 'ambiguous', %s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
)
_DEAD = (
    f"INSERT INTO {DEAD_LETTERS} (repo_id, stage, subject_ref, commit_sha, error_class, "
    "error_message, payload) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)"
)

Cache = dict[str, tuple[HashLadder, IdentityHashes]]


class LineageRefused(RuntimeError):
    """The build will not run; nothing was written."""


@dataclass(frozen=True)
class LineageStats:
    files: int
    revisions: int
    refused: int
    lineages: int
    versions: int
    moves: int
    ambiguous: int

    def summary(self) -> str:
        return (
            f"lineage: {self.files} files, {self.revisions} revisions ({self.refused} refused); "
            f"{self.lineages} identities, {self.versions} versions, {self.moves} moves between "
            f"files, {self.ambiguous} ambiguous cases left unlinked"
        )


def _source(lines: Sequence[str], first: int, last: int) -> str:
    """One part's source as the parser sees it: from its first token to its last line."""
    chunk = list(lines[first - 1 : last])
    if chunk:
        chunk[0] = chunk[0].lstrip()
    return "\n".join(chunk)


def symbols_at(text: str, path: str, cache: Cache) -> dict[str, Symbol] | None:
    """Each function at one revision, hashed over its real source; None when refused."""
    spans = symbol_spans(text, path)
    if spans is None:
        return None
    lines = text.split("\n")
    found: dict[str, Symbol] = {}
    for name, parts in spans.items():
        source = "\n".join(_source(lines, first, last) for first, last in parts)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if digest not in cache:
            cache[digest] = (hash_ladder(source), identity_hashes(source))
        ladder, blind = cache[digest]
        found[name] = Symbol(name, (parts[0][0], parts[-1][1]), ladder, blind.h1, blind.h3)
    return found


def file_passes(conn: Any, repo: Path, repo_id: int) -> tuple[dict[int, FilePass], int]:
    """Pass 1 over every in-scope file; also how many revisions were read."""
    rows = conn.execute(_REVISIONS, (repo_id,)).fetchall()
    if not rows:
        raise LineageRefused("this repository has no file_revisions: run `pace ingest` first")
    cache: Cache = {}
    passes: dict[int, FilePass] = {}
    with CatFileBatch(repo) as cat:
        for file_id, group in groupby(rows, key=lambda row: int(row[0])):
            revisions: list[Revision] = []
            for _, sha, topo, change, blob, path in group:
                symbols: dict[str, Symbol] | None = {}
                if change != "deleted":
                    data = cat.read(str(blob))
                    text = None if data is None else data.decode("utf-8", errors="replace")
                    symbols = None if text is None else symbols_at(text, str(path), cache)
                revisions.append(Revision(str(sha), int(topo), str(path), symbols))
            passes[file_id] = walk_file(file_id, revisions)
    return passes, len(rows)


def _write(conn: Any, repo_id: int, passes: dict[int, FilePass], linked: Linked) -> tuple[int, int]:
    conn.execute(f"DELETE FROM {SYMBOL_LINEAGE} WHERE repo_id = %s", (repo_id,))
    conn.execute(f"DELETE FROM {DEAD_LETTERS} WHERE repo_id = %s AND stage = %s", (repo_id, STAGE))
    ids: dict[int, int] = {}
    versions = 0
    for lineage in linked.lineages:
        birth, last, death = lineage.birth, lineage.last, lineage.death
        alive = death is None
        died_at, died_topo = (None, None) if death is None else death
        kind = "method" if "." in last.symbol.name else "function"
        (lineage_id,) = conn.execute(
            _LINEAGE,
            (
                repo_id,
                kind,
                birth.commit,
                birth.topo,
                died_at,
                died_topo,
                last.path if alive else None,
                last.symbol.name if alive else None,
                len(lineage.versions),
                DETECTOR,
            ),
        ).fetchone()
        for fragment in lineage.fragments:
            ids[id(fragment)] = int(lineage_id)
        rows = [
            (
                lineage_id,
                v.topo,
                repo_id,
                v.commit,
                v.path,
                v.symbol.name,
                v.symbol.span[0],
                v.symbol.span[1],
                v.symbol.ladder.h0,
                v.symbol.ladder.h1,
                v.symbol.ladder.h2,
                v.symbol.ladder.h3,
                v.symbol.ladder.ast_norm_version,
                v.stage,
                1.0,
                v.is_rename,
                v.is_move,
                v.is_body_change,
            )
            for v in lineage.versions
        ]
        with conn.cursor() as cur:
            cur.executemany(_VERSION, rows)
        versions += len(rows)
    cases: list[tuple[Ambiguity, str]] = [
        (ambiguity, "lineage-pass-1") for p in passes.values() for ambiguity in p.ambiguities
    ] + [(ambiguity, "lineage-pass-2") for ambiguity in linked.ambiguities]
    for ambiguity, detector in cases:
        evidence = json.dumps({"stage": ambiguity.stage, "key": ambiguity.key}, sort_keys=True)
        for old in ambiguity.vanished:
            for new in ambiguity.appeared:
                conn.execute(
                    _EDGE,
                    (
                        repo_id,
                        ids[id(old)],
                        ids[id(new)],
                        ambiguity.commit,
                        ambiguity.topo,
                        1 / len(ambiguity.appeared),
                        detector,
                        DETECTOR,
                        evidence,
                    ),
                )
    for file_pass in passes.values():
        for revision in file_pass.refused:
            conn.execute(
                _DEAD,
                (
                    repo_id,
                    STAGE,
                    revision.path,
                    revision.commit,
                    "ChunkParseError",
                    "the chunker refused this revision",
                    json.dumps({"topo": revision.topo}),
                ),
            )
    return versions, len(cases)


def build(conn: Any, *, repo: Path, repo_id: int) -> LineageStats:
    """Rebuild the repository's lineage from scratch, in one transaction."""
    _assert_not_shallow(repo)
    passes, revisions = file_passes(conn, repo, repo_id)
    linked = link_moves(passes)
    try:
        versions, ambiguous = _write(conn, repo_id, passes, linked)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return LineageStats(
        files=len(passes),
        revisions=revisions,
        refused=sum(len(p.refused) for p in passes.values()),
        lineages=len(linked.lineages),
        versions=versions,
        moves=linked.moves,
        ambiguous=ambiguous,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`pace lineage build`: Airflow's function identities, from scratch."""
    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    parser = argparse.ArgumentParser(
        prog="pace lineage build",
        description="Walk every in-scope file's history and store each function's identity "
        "and versions (slice C3a).",
    )
    parser.add_argument("--corpus", default=None, help="corpus path (default: settings)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    corpus = Path(args.corpus) if args.corpus else Path(settings.corpus_path)
    try:
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            stats = build(conn, repo=corpus, repo_id=repo_id)
    except (UnknownRepository, RuntimeError) as exc:  # LineageRefused, a shallow clone
        print(f"LINEAGE REFUSED: {exc}", file=sys.stderr)
        return 1
    print(stats.summary())
    return 0
