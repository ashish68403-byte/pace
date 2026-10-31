"""ADR-0004 §3-§4: one record's gold on screen, and the two-step labelling session.

`pace audit label` and `pace audit relabel` show a record's gold -- the commit message,
then the PR's description and, on request, its comments -- and, on request, the
function's own change. The tool never proposes a label: the labels are the author's.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from provenance.eval.gold_validity import (
    CLAIMS,
    LABELS,
    MENTIONS,
    PR_STRATA,
    RATIONALE,
    UNRELATED,
    LabelRecord,
    SampledRecord,
)

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
