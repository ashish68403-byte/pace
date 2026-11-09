r"""Slice C3a's fixture repository: renames, a move between files, a directory move, an ambiguity.

c1  init                      pkg/core.py: add, scale, helper; pkg/util.py: unused
c2  comment in add            no token changes: no version
c3  docstring of add          a version with a body change
c4  rename add to plus        a rename with an unchanged body: stage h1
c5  rename scale and locals   scale -> multiply, its locals renamed: stage h3
c6  move helper to util       helper leaves core.py, appears in util.py: stage move
c7  move pkg into src         every function's path changes: a version each
c8  Apply ruff formatting     mechanical: `a + b` -> `(a + b)` in plus, a version kept
c9  split unused in two       unused vanishes; zero_a and zero_b appear with its body: ambiguous
c10 break core                core.py does not parse: refused, its state carried
c11 restore core              core.py as at c8 again: no version
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

SCOPE = ("src/pkg/core.py", "src/pkg/util.py")

CORE_V1 = (
    'def add(a, b):\n    """Sum two numbers."""\n    return a + b\n\n\n'
    "def scale(values, factor):\n    out = []\n    for value in values:\n"
    "        out.append(value * factor)\n    return out\n\n\n"
    'def helper():\n    return "help"\n'
)
CORE_C2 = CORE_V1.replace("    return a + b\n", "    # adds them\n    return a + b\n")
CORE_C3 = CORE_C2.replace("Sum two numbers.", "Add two numbers.")
CORE_C4 = CORE_C3.replace("def add(a, b):", "def plus(a, b):")
CORE_C5 = CORE_C4.replace(
    "def scale(values, factor):\n    out = []\n    for value in values:\n"
    "        out.append(value * factor)\n    return out\n",
    "def multiply(items, k):\n    result = []\n    for item in items:\n"
    "        result.append(item * k)\n    return result\n",
)
CORE_C6 = CORE_C5.replace('\n\ndef helper():\n    return "help"\n', "")
CORE_C8 = CORE_C6.replace("    return a + b\n", "    return (a + b)\n")
UTIL_V1 = "def unused():\n    return 0\n"
UTIL_C6 = UTIL_V1 + '\n\ndef helper():\n    return "help"\n'
UTIL_C9 = (
    "def zero_a():\n    return 0\n\n\ndef zero_b():\n    return 0\n\n\n"
    'def helper():\n    return "help"\n'
)
BROKEN = "def (:\n  ))) ((( @@@ $$$ ??? !!!\n  ]]] [[[ }}} {{{\n"


@dataclass(frozen=True)
class LineageRepo:
    root: Path
    sha: dict[str, str]


def _git(root: Path, *args: str, day: int | None = None) -> str:
    env = dict(os.environ)
    if day is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"2022-02-{day:02d}T12:00:00+00:00"
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout.strip()


def make_lineage_repo(root: Path) -> LineageRepo:
    """Build the repository above in `root`; the caller makes git's config hermetic."""
    root.mkdir(parents=True)
    sha: dict[str, str] = {}
    day = 0

    def write(rel: str, text: str) -> None:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(name: str, message: str) -> None:
        nonlocal day
        day += 1
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", message, day=day)
        sha[name] = _git(root, "rev-parse", "HEAD")

    _git(root, "init", "-q", "-b", "main")
    write("pkg/core.py", CORE_V1)
    write("pkg/util.py", UTIL_V1)
    commit("c1", "init")
    for name, message, text in (
        ("c2", "comment in add", CORE_C2),
        ("c3", "docstring of add", CORE_C3),
        ("c4", "rename add to plus", CORE_C4),
        ("c5", "rename scale and its locals", CORE_C5),
    ):
        write("pkg/core.py", text)
        commit(name, message)
    write("pkg/core.py", CORE_C6)
    write("pkg/util.py", UTIL_C6)
    commit("c6", "move helper to util")
    (root / "src").mkdir()
    _git(root, "mv", "pkg", "src/pkg")
    commit("c7", "move pkg into src")
    write("src/pkg/core.py", CORE_C8)
    commit("c8", "Apply ruff formatting")
    write("src/pkg/util.py", UTIL_C9)
    commit("c9", "split unused in two")
    write("src/pkg/core.py", BROKEN)
    commit("c10", "break core")
    write("src/pkg/core.py", CORE_C8)
    commit("c11", "restore core")
    return LineageRepo(root=root, sha=sha)
