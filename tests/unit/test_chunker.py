"""Chunker behaviour, against a small committed fixture.

Deliberately NOT against the live corpus. `~/corpus/airflow` is gitignored, is
absent on a fresh clone and on every CI runner, and changes underneath the test
whenever the pinned commit moves -- so a test written against it is a test that
is skipped in practice and rots in place. The fixture below is ~40 lines and
exercises every rule the chunker claims: a licence header, a module preamble, an
enum-shaped class, a class with methods, and a module-level function.

The fixture lives in this file rather than in a separate `.py` under `tests/` for
one practical reason: a fixture that is itself a valid Python module gets picked
up by linting, by `mypy provenance`-adjacent tooling and (with the wrong glob) by
pytest collection. As a string it is inert, and `chunk_file` is still exercised by
writing it to `tmp_path`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from provenance.ingest.scope import LICENCE_MARKER, strip_licence_header
from provenance.parse.chunker import (
    Chunk,
    chunk_file,
    chunk_source,
    lexical_tokens,
    split_identifier,
)

PATH = "airflow/jobs/scheduler_job_runner.py"
REPO = "apache/airflow"

FIXTURE_SOURCE = '''\
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.

from __future__ import annotations

from enum import Enum


class DAGRunState(str, Enum):
    """The states a DAG run may occupy."""

    QUEUED = "queued"
    RUNNING = "running"


class SchedulerJobRunner:
    """Owns the scheduling loop."""

    heartrate = 5.0

    def __init__(self, job):
        self.job = job
        self.zombies = []

    def _find_zombies(self, session):
        """Task instances whose heartbeat expired while the state says running."""
        limit = self.heartrate * 10
        stale = [ti for ti in session.stale_task_instances() if ti.age > limit]
        for task_instance in stale:
            self.zombies.append(task_instance)
        return self.zombies


def compute_state_summary(runs):
    """Count runs by DAGRunState."""
    summary = {}
    for run in runs:
        summary[run.state] = summary.get(run.state, 0) + 1
    return summary
'''

SUMMARY_DEF_LINE = "def compute_state_summary(runs):"


@pytest.fixture(scope="module")
def chunks() -> list[Chunk]:
    return chunk_source(FIXTURE_SOURCE, PATH, repo=REPO)


def _by_qname(chunks: list[Chunk], qualified_name: str) -> Chunk:
    matches = [c for c in chunks if c.qualified_name == qualified_name]
    assert matches, f"no chunk for {qualified_name}: {[c.qualified_name for c in chunks]}"
    assert len(matches) == 1, f"{qualified_name} produced {len(matches)} chunks"
    return matches[0]


# ------------------------------------------------------------------ licence header


def test_licence_header_is_stripped_before_hashing(chunks: list[Chunk]) -> None:
    """~16 identical lines x 605 files. Hashed, short chunks collide on one id;
    embedded, every query partially matches the same dense junk neighbourhood."""
    assert LICENCE_MARKER in FIXTURE_SOURCE  # the fixture really has a header
    assert all(LICENCE_MARKER not in c.content for c in chunks)
    assert all("Apache License, Version 2.0" not in c.content for c in chunks)


def test_line_numbers_still_address_the_real_file(chunks: list[Chunk]) -> None:
    """The header offset is added back, so a citation points at the file on disk.

    Off-by-header is the kind of bug that produces citations which are plausible,
    consistently wrong by exactly 14 lines, and never noticed until someone opens
    one.
    """
    _, header_lines = strip_licence_header(FIXTURE_SOURCE)
    assert header_lines > 0

    expected = FIXTURE_SOURCE.split("\n").index(SUMMARY_DEF_LINE) + 1
    summary = _by_qname(chunks, "compute_state_summary")
    assert summary.start_line == expected
    assert summary.end_line >= summary.start_line


# ------------------------------------------------------------------ AST boundaries


def test_no_function_is_split_across_chunks(chunks: list[Chunk]) -> None:
    """A whole function, or nothing. The failure this prevents: the half with the
    `if` and the half with the comment explaining the `if` land in different
    chunks, and neither one answers the question."""
    zombies = _by_qname(chunks, "SchedulerJobRunner._find_zombies")
    assert zombies.kind == "method"
    assert zombies.part_total == 1
    for line in (
        "limit = self.heartrate * 10",
        "stale = [ti for ti in session.stale_task_instances() if ti.age > limit]",
        "self.zombies.append(task_instance)",
        "return self.zombies",
    ):
        assert line in zombies.content


def test_every_whole_function_chunk_parses_on_its_own(chunks: list[Chunk]) -> None:
    """The strongest available statement of "never cut mid-function": a chunk cut
    inside a body is not valid Python and `ast.parse` says so."""
    whole = [c for c in chunks if c.kind in {"function", "method"} and c.part_total == 1]
    assert whole, "the fixture must produce at least one whole function chunk"
    for chunk in whole:
        ast.parse(chunk.content)  # raises SyntaxError if the body was cut


def test_class_card_summarises_without_swallowing_method_bodies(chunks: list[Chunk]) -> None:
    card = _by_qname(chunks, "SchedulerJobRunner")
    assert card.kind == "class"
    assert card.synthesized is True
    assert "class SchedulerJobRunner:" in card.content
    assert "heartrate = 5.0" in card.content
    assert "def _find_zombies(self, session):" in card.content
    # the method's BODY belongs to the method chunk, not to the card
    assert "return self.zombies" not in card.content


def test_oversize_function_splits_at_statement_boundaries_with_unique_ids() -> None:
    """When a function genuinely cannot fit, fragments carry the signature and get
    distinct ids. Sharing a qualified_name, fragments would collapse onto one id if
    the part index were left out of the hash -- silently losing all but one."""
    small = chunk_source(FIXTURE_SOURCE, PATH, repo=REPO, max_tokens=8)
    fragments = [c for c in small if c.qualified_name == "SchedulerJobRunner._find_zombies"]

    assert len(fragments) > 1
    assert {c.kind for c in fragments} == {"fragment"}
    assert {c.part_total for c in fragments} == {len(fragments)}
    assert sorted(c.part_index for c in fragments) == list(range(len(fragments)))
    assert len({c.chunk_id for c in fragments}) == len(fragments)
    for fragment in fragments:
        assert fragment.content.startswith("def _find_zombies(self, session):")


# ------------------------------------------------------------------------ identity


def test_chunk_ids_are_unique_within_a_file(chunks: list[Chunk]) -> None:
    ids = [c.chunk_id for c in chunks]
    assert len(set(ids)) == len(ids)
    assert all(len(i) == 64 for i in ids)


def test_chunking_is_deterministic_across_two_runs(chunks: list[Chunk]) -> None:
    again = chunk_source(FIXTURE_SOURCE, PATH, repo=REPO)
    assert [c.chunk_id for c in again] == [c.chunk_id for c in chunks]
    assert [c.qualified_name for c in again] == [c.qualified_name for c in chunks]
    assert [c.lexical_tokens for c in again] == [c.lexical_tokens for c in chunks]


def test_crlf_checkout_produces_the_same_ids(chunks: list[Chunk]) -> None:
    """A Windows checkout must not re-chunk the file. `graph.ids.normalize_content`
    folds the line endings; this asserts the chunker actually routes through it."""
    crlf = chunk_source(FIXTURE_SOURCE.replace("\n", "\r\n"), PATH, repo=REPO)
    assert {c.chunk_id for c in crlf} == {c.chunk_id for c in chunks}


def test_chunk_file_matches_chunk_source(tmp_path: Path, chunks: list[Chunk]) -> None:
    target = tmp_path / "scheduler_job_runner.py"
    target.write_text(FIXTURE_SOURCE, encoding="utf-8")
    from_disk = chunk_file(target, rel_path=PATH, repo=REPO)
    assert [c.chunk_id for c in from_disk] == [c.chunk_id for c in chunks]


# -------------------------------------------------------------------- tokenisation


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("_normalise_url", ["normalise", "url"]),
        ("camelCaseName", ["camel", "case", "name"]),
        ("scheduler_job_runner", ["scheduler", "job", "runner"]),
        # Consecutive capitals: the acronym is its own part, and the word that
        # follows it keeps its leading capital. A naive `(?<!^)(?=[A-Z])` split
        # gives ['d','a','g','run','state'], which matches nothing.
        ("DAGRunState", ["dag", "run", "state"]),
        ("HTTPSConnection", ["https", "connection"]),
        ("TaskInstance2", ["task", "instance2"]),
    ],
)
def test_split_identifier_handles_consecutive_capitals(
    identifier: str, expected: list[str]
) -> None:
    assert split_identifier(identifier) == expected


def test_camel_and_snake_spellings_split_to_the_same_parts() -> None:
    """Why the split matters: a question that says "dag run state" and code that
    says `DAGRunState` have to meet somewhere, and this is where."""
    assert split_identifier("DAGRunState") == split_identifier("dag_run_state")


def test_lexical_tokens_include_the_underscore_free_joined_form() -> None:
    """Measured: `'_normalise_url'::pdb.source_code::text[]` -> `{normalise,url}`.

    The whole identifier does NOT survive ParadeDB's tokenizer, so a query for
    `normaliseurl` finds nothing unless the ingester emits the joined form itself.
    That emission is this function, and `chunks.lexical_blob` is where it lands.
    """
    tokens = lexical_tokens("_normalise_url")
    assert tokens[:2] == ["normalise", "url"]
    assert "normaliseurl" in tokens

    assert "dagrunstate" in lexical_tokens("class DAGRunState(str, Enum):")


def test_lexical_tokens_are_order_preserving_and_deduped() -> None:
    """A re-ingest of an unchanged file must produce an unchanged row, so the
    array cannot be built from a set."""
    tokens = lexical_tokens("dag_run dag_run other_thing")
    assert tokens == ["dag", "run", "dagrun", "other", "thing", "otherthing"]


def test_chunk_lexical_tokens_carry_the_joined_form(chunks: list[Chunk]) -> None:
    zombies = _by_qname(chunks, "SchedulerJobRunner._find_zombies")
    assert "findzombies" in zombies.lexical_tokens
    assert "find" in zombies.lexical_tokens
    assert "zombies" in zombies.lexical_tokens
