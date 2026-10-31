"""ADR-0004 §3-§4: one record's gold on screen, and the two-step labelling session.

`pace audit label` and `pace audit relabel` show a record's gold -- the commit message,
then the PR's description and, on request, its comments -- and, on request, the
function's own change. The tool never proposes a label: the labels are the author's.
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

from provenance.config import settings
from provenance.eval.gold_validity import (
    CLAIMS,
    GOLDEN_PATH,
    LABELS,
    LABELS_PATH,
    MENTIONS,
    PR_STRATA,
    RATIONALE,
    RELABELS_PATH,
    SAMPLE_PATH,
    UNRELATED,
    AuditError,
    LabelRecord,
    SampledRecord,
    append_label,
    check_fresh,
    check_relabel_allowed,
    load_labels,
    load_sample,
)
from provenance.eval.schema import GoldenRecord, QueryClass, load_golden_set
from provenance.graph.tables import CHUNKS, COMMITS, FILE_REVISIONS, FILES
from provenance.parse.symbols import symbols

CODEBOOK: Final = """\
1 rationale  the text gives a reason for this function's change: a bug it fixes, a need it
             serves, a design choice, a requirement or decision it implements. A reason for
             the whole commit or PR counts when this function's change is plausibly part of
             what it explains (d shows the change).
2 mentions   the text says what changed in a way that covers this function, but not why.
3 unrelated  the text neither explains nor describes this function's change.
When torn between two labels, take the one that claims less (ADR-0004 §3)."""
KEYS: Final = {"1": RATIONALE, "2": MENTIONS, "3": UNRELATED}
WIDTH: Final = 78


@dataclass(frozen=True)
class GoldView:
    """What the labeller sees of one record."""

    qid: str
    stratum: str
    question: str
    path: str
    qualified_name: str
    commit: str
    committed_on: str
    message: str
    change: str
    pr: int | None = None
    description: str | None = None
    reviews: tuple[str, ...] = ()
    comments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Screen:
    """A session's two channels: `ask` shows a prompt and reads a line; `show` prints."""

    ask: Callable[[str], str]
    show: Callable[[str], None]


class Quit(Exception):
    """The labeller typed q: stop, writing nothing for the record on screen."""


@dataclass
class _Reading:
    change: bool = False
    comments: bool = False


def _section(title: str, text: str) -> str:
    head = f"── {title} "
    return f"{head}{'─' * max(4, WIDTH - len(head))}\n{text.rstrip()}\n{'─' * WIDTH}"


def render_header(view: GoldView, position: str) -> str:
    kind = f"PR #{view.pr}" if view.pr is not None else "commit message only"
    return "\n".join(
        [
            "",
            f"[{position}]  {view.stratum} · {kind}    {view.qid}",
            view.question,
            f"  function  {view.path} :: {view.qualified_name}",
            f"  commit    {view.commit[:12]}  {view.committed_on}",
        ]
    )


def render_pr(view: GoldView) -> str:
    text = view.description or "(this PR has no description in the pool)"
    counts = (
        f"also {len(view.reviews)} review and {len(view.comments)} conversation comments "
        "in the pool: c reads them"
    )
    return f"{_section(f'PR #{view.pr} description', text)}\n{counts}"


def render_comments(view: GoldView) -> str:
    parts = [_section(f"review comment {i}", t) for i, t in enumerate(view.reviews, 1)]
    parts += [_section(f"conversation comment {i}", t) for i, t in enumerate(view.comments, 1)]
    return "\n".join(parts) or "(no comments in the pool)"


def _choose(
    screen: Screen,
    view: GoldView,
    prompt: str,
    allowed: Sequence[str],
    reading: _Reading,
    *,
    comments: bool,
) -> str:
    """Ask until the answer is an allowed label. d, c, ? and q are answered here."""
    keys = {key: label for key, label in KEYS.items() if label in allowed}
    menu = " · ".join(f"{key} {label}" for key, label in keys.items())
    extra = "d change" + (" · c comments" if comments else "") + " · ? codebook · q quit"
    hint = f"  type one of: {', '.join(keys)}, d{', c' if comments else ''}, ? or q"
    while True:
        answer = screen.ask(f"{prompt}  {menu}   ({extra}) > ").strip().lower()
        if answer in keys:
            return keys[answer]
        if answer == "q":
            raise Quit
        if answer == "?":
            screen.show(CODEBOOK)
        elif answer == "d":
            reading.change = True
            screen.show(_section("the function's change", view.change))
        elif answer == "c" and comments:
            reading.comments = True
            screen.show(render_comments(view))
        else:
            screen.show(hint)


def label_one(screen: Screen, view: GoldView, position: str, today: date) -> LabelRecord:
    """ADR-0004 §4's two steps for one record. Raises Quit having written nothing."""
    reading = _Reading()
    screen.show(render_header(view, position))
    screen.show(_section("commit message", view.message))
    first = _choose(screen, view, "The commit message alone:", LABELS, reading, comments=False)
    final = first
    linked = view.stratum in PR_STRATA
    if linked and first != RATIONALE:
        screen.show(render_pr(view))
        stronger = [label for label in LABELS if CLAIMS[label] >= CLAIMS[first]]
        final = _choose(screen, view, "With the PR as well:", stronger, reading, comments=True)
    note = screen.ask("note, optional (Enter to skip) > ").strip()
    return LabelRecord(
        qid=view.qid,
        stratum=view.stratum,
        message_label=first,
        label=final,
        note=note or None,
        labelled_on=today,
        read_change=reading.change,
        read_comments=reading.comments if linked else None,
    )


def run_session(
    items: Sequence[SampledRecord],
    done: Collection[str],
    *,
    view: Callable[[SampledRecord], GoldView],
    screen: Screen,
    today: date,
    write: Callable[[LabelRecord], None],
    title: str,
) -> int:
    """Label every item not yet done, in order, writing each at once. Returns how many."""
    already = sum(1 for item in items if item.qid in done)
    written = 0
    for item in items:
        if item.qid in done:
            continue
        position = f"{title} {already + written + 1}/{len(items)}"
        try:
            record = label_one(screen, view(item), position, today)
        except Quit:
            screen.show(f"stopped at {already + written} of {len(items)}; run it again to go on")
            return written
        write(record)
        written += 1
    screen.show(f"all {len(items)} labelled")
    return written


# ------------------------------------------------------------------ from the database

_DOCUMENTS_SQL: Final = f"SELECT kind::text, path, content FROM {CHUNKS} WHERE chunk_id = ANY(%s)"
_COMMITTED_SQL: Final = (
    f"SELECT committed_at::date FROM {COMMITS} WHERE repo_id = %s AND commit_sha = %s"
)
_REVISION_SQL: Final = f"""
    SELECT r.blob_sha, r.path_at_commit, r.commit_topo_order, r.file_id
    FROM {FILE_REVISIONS} r JOIN {FILES} f ON f.file_id = r.file_id
    WHERE r.repo_id = %s AND f.path = %s AND r.commit_sha = %s
"""
_PREVIOUS_SQL: Final = f"""
    SELECT blob_sha, path_at_commit, change_type::text FROM {FILE_REVISIONS}
    WHERE file_id = %s AND commit_topo_order < %s
    ORDER BY commit_topo_order DESC LIMIT 1
"""


def load_view(
    conn: Any, repo_id: int, corpus: Path, record: GoldenRecord, stratum: str
) -> GoldView:
    """A record's gold documents, grouped by kind, and its function's change."""
    from provenance.eval.leakage import resolve_gold_from_db

    commit = record.required_evidence.commit_shas[0]
    anchor = record.supporting_evidence.symbol_anchors[0]
    gold = sorted(resolve_gold_from_db(record, conn, repo_id).chunk_ids)
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for kind, path, content in conn.execute(_DOCUMENTS_SQL, (gold,)).fetchall():
        by_kind.setdefault(kind, []).append((path, content))
    messages = [content for _, content in sorted(by_kind.get("commit", []))]
    descriptions = [content for _, content in sorted(by_kind.get("pull_request", []))]
    (committed,) = conn.execute(_COMMITTED_SQL, (repo_id, commit)).fetchone()
    prs = record.required_evidence.pr_numbers
    return GoldView(
        qid=record.qid,
        stratum=stratum,
        question=record.question,
        path=anchor.path,
        qualified_name=anchor.qualified_name or "",
        commit=commit,
        committed_on=committed.isoformat(),
        message="\n\n".join(messages),
        change=function_change(conn, repo_id, corpus, record),
        pr=prs[0] if prs else None,
        description="\n\n".join(descriptions) if descriptions else None,
        reviews=tuple(c for _, c in sorted(by_kind.get("review_comment", []), key=_by_id)),
        comments=tuple(c for _, c in sorted(by_kind.get("issue_comment", []), key=_by_id)),
    )


def _by_id(item: tuple[str, str]) -> int:
    """`pull/14/comment/4` sorts by its comment id, not as text."""
    return int(item[0].rsplit("/", 1)[-1])


def function_change(conn: Any, repo_id: int, corpus: Path, record: GoldenRecord) -> str:
    """The function's source at the commit when it was added; when it changed, the diff of
    its source against the file's previous revision. Read from the corpus by blob."""
    anchor = record.supporting_evidence.symbol_anchors[0]
    name = anchor.qualified_name or ""
    commit = record.required_evidence.commit_shas[0]
    row = conn.execute(_REVISION_SQL, (repo_id, anchor.path, commit)).fetchone()
    if row is None:
        return f"(no revision of {anchor.path} at {commit[:12]})"
    blob, path_at_commit, topo, file_id = row
    after = _function(corpus, blob, path_at_commit, name)
    if after is None:
        return f"({name} could not be read at {commit[:12]})"
    if record.query_class is QueryClass.PROVENANCE:
        return after
    previous = conn.execute(_PREVIOUS_SQL, (file_id, topo)).fetchone()
    before = None
    if previous is not None and previous[2] != "deleted":
        before = _function(corpus, previous[0], previous[1], name)
    if before is None:
        return after
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(), "before", "after", lineterm=""
    )
    return "\n".join(diff)


def _function(corpus: Path, blob: str, path: str, name: str) -> str | None:
    done = subprocess.run(
        ["git", "-C", str(corpus), "cat-file", "blob", blob], capture_output=True, check=False
    )
    if done.returncode != 0:
        return None
    found = symbols(done.stdout.decode("utf-8", "replace"), path)
    return None if found is None else found.get(name)


# ------------------------------------------------------------------------ commands


def label_main(argv: Sequence[str] | None = None) -> int:
    """`pace audit label`: label the sample, resumably (ADR-0004 §3-§4)."""
    return _main("pace audit label", argv, relabel=False)


def relabel_main(argv: Sequence[str] | None = None) -> int:
    """`pace audit relabel`: the 15-record subset again, a week or more later (ADR-0004 §6)."""
    return _main("pace audit relabel", argv, relabel=True)


def _main(prog: str, argv: Sequence[str] | None, *, relabel: bool) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--corpus", default=None, help="corpus path (default: settings)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    corpus = Path(args.corpus) if args.corpus else Path(settings.corpus_path)
    return run(corpus, relabel=relabel, today=date.today(), screen=Screen(ask=input, show=print))


def run(corpus: Path, *, relabel: bool, today: date, screen: Screen) -> int:
    """Check the sample is fresh, then run the session over the labels or the subset."""
    from provenance.eval.generator import pool_version
    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    try:
        sample = load_sample(SAMPLE_PATH)
        records = load_golden_set(GOLDEN_PATH)
        if relabel:
            check_relabel_allowed(sample, load_labels(LABELS_PATH), today)
            items, out, title = sample.relabel_records(), RELABELS_PATH, "relabel"
        else:
            items, out, title = list(sample.records), LABELS_PATH, "label"
        done = set(load_labels(out))
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            fresh = check_fresh(sample, records, pool_version(conn, repo_id))

            def view(item: SampledRecord) -> GoldView:
                return load_view(conn, repo_id, corpus, fresh[item.qid], item.stratum)

            run_session(
                items,
                done,
                view=view,
                screen=screen,
                today=today,
                write=lambda record: append_label(out, record),
                title=title,
            )
    except UnknownRepository as exc:
        print(f"nothing to label: {exc}", file=sys.stderr)
        return 2
    except AuditError as exc:
        print(f"AUDIT REFUSED: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nstopped: nothing was written for the record on screen")
    return 0
