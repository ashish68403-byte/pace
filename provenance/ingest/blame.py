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

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from typing import Final

#: Plain blame. `--first-parent`: a side branch's change belongs to its merge, as the gold
#: does. `-w`: whitespace changes own nothing. `-M`: lines moved within the file keep their
#: owner. No `-C` (ADR-0005 §6).
BLAME_FLAGS: Final = ("--porcelain", "-w", "-M", "--first-parent")
#: `git log -L`, one SHA per listed commit and no patch.
LOG_FLAGS: Final = ("--first-parent", "--format=%H", "-s")

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
