"""The chunk identity contract.

`provenance/graph/ids.py` is the load-bearing floor of the whole eval: the golden
set anchors on commits and (path, qualified_name) and resolves those to chunk ids
at scoring time, which is only worth anything if a chunk id is a deterministic
function of content. Every assertion below is one clause of that contract, and
every one of them fails *silently* if it regresses -- a salted hash still returns
64 hex characters, and an id that folds in the chunker version still looks like
an id. The only symptom is recall collapsing after an unrelated change.

The version clause is asserted BY COMPUTING, never by reading the source: a test
that greps `ids.py` for the string `chunking_strategy_version` passes just as
happily when the version is folded into the hash through a different name.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from provenance.config import settings
from provenance.graph.ids import CHUNK_ID_SCHEME, chunk_id, normalize_content

REPO = "apache/airflow"
PATH = "airflow/jobs/scheduler_job_runner.py"
QNAME = "SchedulerJobRunner._find_zombies"
BODY = "def _find_zombies(self):\n    return self.zombies\n"


# --------------------------------------------------------------------- normalisation


def test_crlf_and_lf_hash_identically() -> None:
    """A Windows checkout must not re-chunk the entire corpus.

    This is not hypothetical: the repo has a `.gitattributes`, but a contributor
    with `core.autocrlf=true` and one file checked out before it landed is enough
    to churn every id in that file.
    """
    lf = "def f():\n    return 1\n"
    crlf = "def f():\r\n    return 1\r\n"
    cr = "def f():\r    return 1\r"

    assert normalize_content(lf) == normalize_content(crlf) == normalize_content(cr)
    assert chunk_id(REPO, PATH, QNAME, lf) == chunk_id(REPO, PATH, QNAME, crlf)
    assert chunk_id(REPO, PATH, QNAME, lf) == chunk_id(REPO, PATH, QNAME, cr)


def test_trailing_whitespace_is_stripped_per_line() -> None:
    """An editor that trims trailing spaces on save must not change any id."""
    dirty = "def f():   \n    return 1\t\n"
    clean = "def f():\n    return 1\n"

    assert normalize_content(dirty) == normalize_content(clean)
    assert chunk_id(REPO, PATH, QNAME, dirty) == chunk_id(REPO, PATH, QNAME, clean)


def test_leading_and_trailing_blank_lines_are_dropped() -> None:
    padded = "\n\n\ndef f():\n    return 1\n\n\n"
    assert normalize_content(padded) == "def f():\n    return 1"


def test_interior_blank_lines_and_indentation_are_preserved() -> None:
    """Normalisation must not become "canonicalise the code".

    Indentation is semantics in Python, and a blank line inside a body is a real
    difference a human made. If either were normalised away, two genuinely
    different chunks would collide on one id.
    """
    body = "def f():\n    a = 1\n\n    return a"
    assert normalize_content(body) == body
    assert chunk_id(REPO, PATH, QNAME, body) != chunk_id(
        REPO, PATH, QNAME, "def f():\n    a = 1\n    return a"
    )


def test_unicode_is_folded_to_nfc() -> None:
    """Composed and decomposed spellings of the same character are one chunk."""
    composed = unicodedata.normalize("NFC", "x = 'café'")
    decomposed = unicodedata.normalize("NFD", "x = 'café'")
    assert composed != decomposed  # the inputs really are different byte strings
    assert chunk_id(REPO, PATH, QNAME, composed) == chunk_id(REPO, PATH, QNAME, decomposed)


# --------------------------------------------------------------------------- shape


def test_id_is_64_lowercase_hex() -> None:
    value = chunk_id(REPO, PATH, QNAME, BODY)
    assert len(value) == 64
    assert set(value) <= set("0123456789abcdef")


def test_id_is_exactly_sha256_over_the_nul_joined_payload() -> None:
    """Pins the payload layout, which is what makes the id reproducible off-box.

    Recomputing the digest here (rather than asserting a frozen literal) means
    this test also proves what is NOT in the payload: five fields, joined on NUL,
    and no room for a sixth.
    """
    payload = "\x00".join([CHUNK_ID_SCHEME, REPO, PATH, QNAME, normalize_content(BODY)])
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert chunk_id(REPO, PATH, QNAME, BODY) == expected


def test_nul_join_prevents_field_boundary_collisions() -> None:
    """("a/b", "c") and ("a", "b/c") must not serialise to the same byte string."""
    left = chunk_id(REPO, "a/b", "c", BODY)
    right = chunk_id(REPO, "a", "b/c", BODY)
    assert left != right


def test_absent_qualified_name_is_the_empty_string_not_the_word_none() -> None:
    """`None` must serialise as "", or a file-level chunk and a chunk whose symbol
    is literally called `None` would collide."""
    payload = "\x00".join([CHUNK_ID_SCHEME, REPO, PATH, "", normalize_content(BODY)])
    assert chunk_id(REPO, PATH, None, BODY) == hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ what changes it


@pytest.mark.parametrize(
    ("repo", "path", "qname", "content"),
    [
        ("apache/airflow-fork", PATH, QNAME, BODY),
        (REPO, "airflow/jobs/other_job_runner.py", QNAME, BODY),
        (REPO, PATH, "SchedulerJobRunner._find_zombies_v2", BODY),
        (REPO, PATH, QNAME, "def _find_zombies(self):\n    return self.zombies_v2\n"),
    ],
    ids=["repo", "path", "qualified_name", "content"],
)
def test_id_changes_on_any_real_field_change(
    repo: str, path: str, qname: str, content: str
) -> None:
    assert chunk_id(repo, path, qname, content) != chunk_id(REPO, PATH, QNAME, BODY)


def test_chunking_strategy_version_is_not_a_hash_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE clause that keeps the golden set alive across a chunker change.

    Fold the derivation version into the hash and the first parser fix rewrites
    every chunk id in the corpus, which invalidates every cached embedding and --
    fatally -- every golden-set anchor. A one-line chunker tweak then costs a full
    re-annotation of the eval.

    Asserted by computing: bump the setting, recompute, demand the same id. Note
    that this passes today because `chunk_id` never reads `settings` at all; the
    test exists so that the day someone "helpfully" adds it, CI says so.
    """
    before = chunk_id(REPO, PATH, QNAME, BODY)
    monkeypatch.setattr(settings, "chunking_strategy_version", "v999-experimental")
    assert settings.chunking_strategy_version == "v999-experimental"
    assert chunk_id(REPO, PATH, QNAME, BODY) == before


def test_embedding_model_version_is_not_a_hash_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same clause, for the other derivation version. Re-embedding with a new
    model must invalidate vectors and nothing else."""
    before = chunk_id(REPO, PATH, QNAME, BODY)
    monkeypatch.setattr(settings, "embedding_model_version", "some-other-model-v9")
    assert chunk_id(REPO, PATH, QNAME, BODY) == before


# ----------------------------------------------------------------- cross-process


def test_id_is_stable_across_interpreters_with_different_hash_seeds() -> None:
    """The builtin `hash()` clause, tested the only way it can honestly be tested.

    `str.__hash__` is salted per process (PYTHONHASHSEED), so an id built on it is
    identical in every unit test in one process and different in the next run.
    Two subprocesses with two explicit, different seeds is what actually catches
    it. If this ever fails, some part of the id payload has picked up a set or a
    dict iteration order, or `hash()` itself.
    """
    repo_root = Path(__file__).resolve().parents[2]
    program = (
        "from provenance.graph.ids import chunk_id;"
        f"print(chunk_id({REPO!r}, {PATH!r}, {QNAME!r}, {BODY!r}))"
    )

    digests = []
    for seed in ("1", "424242"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(repo_root))
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(repo_root),
            env=env,
        )
        digests.append(result.stdout.strip())

    assert digests[0] == digests[1]
    assert digests[0] == chunk_id(REPO, PATH, QNAME, BODY)
