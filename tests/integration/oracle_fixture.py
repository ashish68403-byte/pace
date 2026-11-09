r"""Slice C3b's fixture: a repository, its bare clone, CodeShovel-shaped oracle files, a pace root.

c1   init      app/views.py: dispatch, View.render, View.title (getter and setter), outer > inner;
               app/util.py: helper, other; lib/old_name.py: parse
c2   edit      dispatch's body, and outer's own first line (not inner)
c3   feature   View.render's body, on a side branch from c2
c4   edit      inner's body
m5   merge     feature, --no-ff: render's change reaches the first-parent line here
c6   move      helper leaves util.py and is appended to views.py, unchanged
c7   rename    lib/old_name.py -> lib/new_name.py, unchanged
c8   edit      the title setter's body, not the getter
c9   comment   a comment in dispatch: no token changes
c10  edit      helper's body: every oracle function's start commit
s1   stray     a commit on a branch from c2 that is never merged: no ancestor of c10

Six oracle files, one per function, labelled by hand as CodeShovel would label them.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VIEWS_C1 = (
    "def dispatch(request):\n    kind = request.kind\n    return kind\n\n\n"
    "class View:\n    def render(self, page):\n        return page.upper()\n\n"
    "    @property\n    def title(self):\n        return self._title\n\n"
    "    @title.setter\n    def title(self, value):\n        self._title = value\n\n\n"
    "def outer(items):\n    total = 0\n\n    def inner(item):\n        return item * 2\n\n"
    "    for item in items:\n        total += inner(item)\n    return total\n"
)
VIEWS_C2 = VIEWS_C1.replace("kind = request.kind\n", "kind = request.kind.lower()\n").replace(
    "    total = 0\n", "    total = 1\n"
)
VIEWS_C3 = VIEWS_C2.replace("return page.upper()", "return page.title()")
VIEWS_C4 = VIEWS_C2.replace("return item * 2", "return item * 3")
VIEWS_M5 = VIEWS_C4.replace("return page.upper()", "return page.title()")
HELPER = "def helper(text):\n    return text.strip()\n"
VIEWS_C6 = VIEWS_M5 + "\n\n" + HELPER
VIEWS_C8 = VIEWS_C6.replace("self._title = value\n", "self._title = value.strip()\n")
VIEWS_C9 = VIEWS_C8.replace(
    "    kind = request.kind.lower()\n",
    "    # the request's kind\n    kind = request.kind.lower()\n",
)
VIEWS_C10 = VIEWS_C9.replace("    return text.strip()\n", "    return text.strip().lower()\n")
UTIL_C1 = HELPER + "\n\ndef other():\n    return None\n"
UTIL_C6 = "def other():\n    return None\n"
PARSE = 'def parse(line):\n    return line.split(",")\n'
FINAL = {"app/views.py": VIEWS_C10, "lib/new_name.py": PARSE}
ORACLE_DIR = Path("src") / "test" / "resources" / "oracles" / "python"

#: Each oracle function: its id, its path at c10, its short name, how its def line starts, and
#: its labels by commit name.
FUNCTIONS: tuple[tuple[str, str, str, str, dict[str, str]], ...] = (
    (
        "fixture-lib-parse",
        "lib/new_name.py",
        "parse",
        "def parse(",
        {"c1": "Yintroduced", "c7": "Yfilerename"},
    ),
    (
        "fixture-views-dispatch",
        "app/views.py",
        "dispatch",
        "def dispatch(",
        {"c1": "Yintroduced", "c2": "Ybodychange"},
    ),
    (
        "fixture-views-helper",
        "app/views.py",
        "helper",
        "def helper(",
        {"c1": "Yintroduced", "c6": "Ymovefromfile", "c10": "Ybodychange"},
    ),
    (
        "fixture-views-inner",
        "app/views.py",
        "inner",
        "def inner(",
        {"c1": "Yintroduced", "c4": "Ybodychange"},
    ),
    (
        "fixture-views-render",
        "app/views.py",
        "render",
        "def render(",
        {"c1": "Yintroduced", "c3": "Ybodychange"},
    ),
    ("fixture-views-title", "app/views.py", "title", "def title(self):", {"c1": "Yintroduced"}),
)


def _git(root: Path, *args: str, day: int | None = None) -> str:
    env = dict(os.environ)
    if day is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"2023-03-{day:02d}T12:00:00+00:00"
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout.strip()


def line_of(text: str, starts: str) -> int:
    """The 1-based line whose code starts with `starts`."""
    lines = enumerate(text.split("\n"), 1)
    return next(number for number, line in lines if line.lstrip().startswith(starts))


@dataclass(frozen=True)
class OracleFixture:
    repo: Path
    clones: Path
    codeshovel: Path
    root: Path
    sha: dict[str, str]

    @property
    def oracle_dir(self) -> Path:
        return self.codeshovel / ORACLE_DIR

    @property
    def codeshovel_commit(self) -> str:
        return _git(self.codeshovel, "rev-parse", "HEAD")

    def names(self, commits: Any) -> set[str]:
        by_sha = {value: key for key, value in self.sha.items()}
        return {by_sha[commit] for commit in commits}

    def rewrite(self, oracle: str, **fields: Any) -> str:
        """Change one oracle file, commit it, and return CodeShovel's new commit."""
        path = self.oracle_dir / f"{oracle}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(fields)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        _git(self.codeshovel, "commit", "-q", "-am", f"rewrite {oracle}", day=22)
        return self.codeshovel_commit


def _build(root: Path) -> dict[str, str]:
    root.mkdir(parents=True)
    sha: dict[str, str] = {}

    def write(rel: str, text: str) -> None:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(name: str, day: int) -> None:
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", name, day=day)
        sha[name] = _git(root, "rev-parse", "HEAD")

    _git(root, "init", "-q", "-b", "main")
    write("app/views.py", VIEWS_C1)
    write("app/util.py", UTIL_C1)
    write("lib/old_name.py", PARSE)
    commit("c1", 1)
    write("app/views.py", VIEWS_C2)
    commit("c2", 2)
    _git(root, "checkout", "-q", "-b", "feature")
    write("app/views.py", VIEWS_C3)
    commit("c3", 3)
    _git(root, "checkout", "-q", "main")
    write("app/views.py", VIEWS_C4)
    commit("c4", 4)
    _git(root, "merge", "--no-ff", "-q", "-m", "m5", "feature", day=5)
    sha["m5"] = _git(root, "rev-parse", "HEAD")
    write("app/views.py", VIEWS_C6)
    write("app/util.py", UTIL_C6)
    commit("c6", 6)
    _git(root, "mv", "lib/old_name.py", "lib/new_name.py")
    commit("c7", 7)
    for name, text, day in (("c8", VIEWS_C8, 8), ("c9", VIEWS_C9, 9), ("c10", VIEWS_C10, 10)):
        write("app/views.py", text)
        commit(name, day)
    _git(root, "checkout", "-q", "-b", "stray", sha["c2"])
    write("app/util.py", UTIL_C1.replace("text.strip()", "text"))
    commit("s1", 11)
    _git(root, "checkout", "-q", "main")
    return sha


def _commit_all(root: Path, message: str, *, day: int) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message, day=day)


def make_oracle_fixture(base: Path) -> OracleFixture:
    """Build everything under `base`; the caller makes git's config hermetic."""
    repo = base / "fixture"
    sha = _build(repo)
    clones = base / "clones"
    clones.mkdir()
    _git(base, "clone", "--bare", "-q", str(repo), str(clones / "fixture.git"))
    codeshovel = base / "codeshovel"
    oracle_dir = codeshovel / ORACLE_DIR
    oracle_dir.mkdir(parents=True)
    for oracle, path, name, starts, labels in FUNCTIONS:
        record = {
            "repositoryName": "fixture",
            "filePath": path,
            "functionName": name,
            "functionStartLine": line_of(FINAL[path], starts),
            "startCommitName": sha["c10"],
            "expectedResult": {sha[commit]: kind for commit, kind in labels.items()},
        }
        (oracle_dir / f"{oracle}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    _commit_all(codeshovel, "the oracle", day=20)
    root = base / "pace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "PREREGISTRATION.md").write_text("# Pre-registration\n", encoding="utf-8")
    _commit_all(root, "pre-register", day=21)
    return OracleFixture(repo=repo, clones=clones, codeshovel=codeshovel, root=root, sha=sha)
