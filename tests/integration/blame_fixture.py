r"""Slice C2's fixture repository: a side branch, a directory move and a mechanical commit.

    main:  c1 -- c2 ------ m3 -- c4 -- c5 -- c6
             \            /
    side:     s1 --------

    c1  init                       pkg/calc.py with total, scale and Box.size
    s1  side: total returns int    `return result` -> `return int(result)`
    c2  edit readme                so that m3 is a real merge
    m3  Merge branch side (#3)     brings s1's line in
    c4  move pkg into src          pkg/ -> src/pkg/, as Airflow's 2025 move did
    c5  Apply ruff formatting      mechanical: changes a line in total and in scale, adds one
    c6  total adds explicitly      `result += value` -> `result = result + value`

At c6, src/pkg/calc.py reads:

     4  def total(values):               c1
     5      result: int = 0              c5 changed `result = 0`
     6      log("hello world and more")  c5 added it, in the same hunk
     7      for value in values:         c1
     8          result = result + value  c6
     9      return int(result)           m3 (s1 on the side branch)
    12  @functools.cache                 c1
    13  def scale(x):                    c1
    14      return (x * 2)               c5 changed `return x * 2`
    18-20 and 22-24: Box.size's getter and setter, c1
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

PATH = "src/pkg/calc.py"
OLD_PATH = "pkg/calc.py"

CALC_V1 = (
    "import functools\n"
    "\n"
    "\n"
    "def total(values):\n"
    "    result = 0\n"
    "    for value in values:\n"
    "        result += value\n"
    "    return result\n"
    "\n"
    "\n"
    "@functools.cache\n"
    "def scale(x):\n"
    "    return x * 2\n"
    "\n"
    "\n"
    "class Box:\n"
    "    @property\n"
    "    def size(self):\n"
    "        return self._size\n"
    "\n"
    "    @size.setter\n"
    "    def size(self, value):\n"
    "        self._size = value\n"
)
SIDE = CALC_V1.replace("    return result\n", "    return int(result)\n")
#: c5 changes `result = 0` and, in the same hunk, adds a line like none before it: git can
#: re-attribute the change but not the addition. A line inserted on its own would go to a
#: neighbouring line's commit instead (checked 2026-11-09).
FORMATTED = SIDE.replace(
    "    result = 0\n", '    result: int = 0\n    log("hello world and more")\n'
).replace("    return x * 2\n", "    return (x * 2)\n")
FINAL = FORMATTED.replace("        result += value\n", "        result = result + value\n")

#: Each function's spans at c6.
SPANS = {"total": ((4, 9),), "scale": ((12, 14),), "Box.size": ((18, 20), (22, 24))}


@dataclass(frozen=True)
class BlameRepo:
    root: Path
    sha: dict[str, str]


def _git(root: Path, *args: str, day: int | None = None) -> str:
    env = dict(os.environ)
    if day is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"2021-01-{day:02d}T12:00:00+00:00"
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout.strip()


def make_blame_repo(root: Path) -> BlameRepo:
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
    write(OLD_PATH, CALC_V1)
    write("README.md", "fixture\n")
    commit("c1", "init")
    _git(root, "switch", "-q", "-c", "side")
    write(OLD_PATH, SIDE)
    commit("s1", "side: total returns int")
    _git(root, "switch", "-q", "main")
    write("README.md", "fixture, edited\n")
    commit("c2", "edit readme")
    day += 1
    _git(root, "merge", "-q", "--no-ff", "side", "-m", "Merge branch side (#3)", day=day)
    sha["m3"] = _git(root, "rev-parse", "HEAD")
    (root / "src").mkdir()
    _git(root, "mv", "pkg", "src/pkg")
    commit("c4", "move pkg into src")
    write(PATH, FORMATTED)
    commit("c5", "Apply ruff formatting")
    write(PATH, FINAL)
    commit("c6", "total adds explicitly")
    return BlameRepo(root=root, sha=sha)
