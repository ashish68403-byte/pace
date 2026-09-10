# ADR-0001: Typed edge tables, not one polymorphic edges table

- **Status:** Accepted
- **Date:** 2026-09-10
- **Deciders:** project author (solo)
- **Supersedes:** none
- **Affects:** `provenance/graph/`, all Alembic revisions, every retrieval traversal

---

## Context

PACE answers "why is this code the way it is?" by traversing a provenance graph from a
code chunk out to the artefacts that explain it. The node types are fixed and small:

| node | grain |
|---|---|
| `chunk` | a span of source with a `chunk_id` (see `provenance/graph/ids.py`) |
| `commit` | a git commit, keyed on `commit_sha` |
| `pull_request` | keyed on `pr_number` |
| `issue` | keyed on `issue_number` |
| `review_comment` | an inline PR review comment, keyed on `review_comment_id` |
| `file` | a repository path |

The edges between them are also fixed, small in number, and — this is the important part —
**each carries different attributes**:

| edge | attributes it must carry |
|---|---|
| commit → file (touches) | added/deleted line counts, rename-from path, hunk ranges |
| commit → chunk (modifies) | the hunk overlap that justified the link, overlap fraction |
| pull_request → commit (contains) | position in the PR, whether it survived a squash |
| pull_request → issue (closes) | which keyword closed it (`fixes` / `closes` / manual) |
| review_comment → chunk (comments on) | original line, current line, diff side, outdated flag |
| issue → issue (references) | reference kind (duplicate, blocks, mentions) |
| commit → person (authored by) | authored-at vs committed-at, hashed email |

The design question at the start of Phase 0 was whether to model these as one generic
table

```sql
CREATE TABLE edge (
    src_type text, src_id text,
    dst_type text, dst_id text,
    edge_type text,
    attributes jsonb
);
```

or as one narrow table per edge type. The decision has to be made **before** any row is
ingested, because it determines the shape of every migration, every query, and every
join in the retrieval layer.

A further constraint: this runs on an i3-1115G4 with 2 physical cores and ~3.7 GB of RAM
visible to WSL. There is no headroom to compensate for a bad query plan with hardware,
and no second machine to fall back on.

## Decision

**One table per edge type, with real foreign keys and real typed columns.**

```sql
CREATE TABLE commit_touches_file (
    commit_sha      text    NOT NULL REFERENCES git_commit(commit_sha) ON DELETE CASCADE,
    path            text    NOT NULL,
    lines_added     integer NOT NULL,
    lines_deleted   integer NOT NULL,
    rename_from     text,
    PRIMARY KEY (commit_sha, path)
);

CREATE TABLE review_comment_on_chunk (
    review_comment_id bigint NOT NULL REFERENCES review_comment(review_comment_id) ON DELETE CASCADE,
    chunk_id          text   NOT NULL REFERENCES chunk(chunk_id) ON DELETE CASCADE,
    original_line     integer,
    current_line      integer,
    diff_side         text   NOT NULL CHECK (diff_side IN ('LEFT', 'RIGHT')),
    is_outdated       boolean NOT NULL DEFAULT false,
    PRIMARY KEY (review_comment_id, chunk_id)
);
```

…and so on, one table per row of the table above. Tables are created by raw SQL inside
Alembic revisions (`op.execute`), matching the project-wide "plain SQL, no ORM" rule.

For the small number of places that genuinely want type-agnostic traversal — the
"expand one hop from anything" step in the retriever, and the graph debug view — a
`UNION ALL` view flattens the typed tables into the generic shape:

```sql
CREATE VIEW edge_all AS
  SELECT 'commit'::text AS src_type, commit_sha AS src_id,
         'file'::text   AS dst_type, path       AS dst_id,
         'touches'::text AS edge_type
    FROM commit_touches_file
  UNION ALL
  SELECT 'review_comment', review_comment_id::text, 'chunk', chunk_id, 'comments_on'
    FROM review_comment_on_chunk
  -- ... one arm per typed table
;
```

The view is derived from the tables, so the tables stay the source of truth and the
generic access pattern costs one view definition rather than a whole data model.

## Consequences

### Positive

- **Referential integrity is enforced by the database, not by hope.** A polymorphic
  `(dst_type, dst_id)` pair cannot have a foreign key, because the target table is not
  known at DDL time. That means a delete or a re-chunk can leave dangling edges, and the
  first symptom is a citation in an answer pointing at a chunk that no longer exists.
  For a system whose entire value proposition is *citing its evidence*, a dangling
  citation is not a bug, it is a refutation. Typed tables make it impossible.
- **Attributes are columns, so they are typed, constrained, and indexable.** `diff_side`
  gets a `CHECK`; `lines_added` gets `NOT NULL integer`. In the polymorphic design these
  live in `jsonb` where `"3"` and `3` are different values and nothing stops either.
- **The planner gets honest statistics.** Per-table `n_distinct` and correlation on a
  narrow table gives sane index selection. A single `edge` table holding seven edge types
  has statistics averaged across all of them, and the planner picks a sequential scan on
  a table that a filtered index would answer in milliseconds. With 2 cores, that is the
  difference between a 40 ms hop and a 4 s hop.
- **`text` ids stop being a lowest common denominator.** `pr_number` stays `integer`,
  `commit_sha` stays a fixed-width `text`, `review_comment_id` stays `bigint`. The
  polymorphic table forces everything to `text` and every join to cast, which silently
  disables index use.
- **Migrations are legible.** Adding `is_outdated` to review-comment edges is one
  `ALTER TABLE`, reviewable in a diff. In the polymorphic design it is an untyped change
  to a `jsonb` blob that no migration can validate and no reviewer can see.
- **Queries say what they mean.** `JOIN review_comment_on_chunk USING (chunk_id)` reads
  as the domain concept. `WHERE edge_type = 'comments_on' AND dst_type = 'chunk'` is
  three predicates standing in for one noun.

### Negative

- **More tables and more migrations.** Seven edge tables instead of one. Mitigated by
  the fact that the edge set is closed by design: the graph models git and GitHub, both
  of which have a fixed vocabulary. New edge types will be rare, and each one deserves a
  migration anyway.
- **Adding an edge type touches DDL.** A polymorphic table lets you invent an edge type
  with an `INSERT`. That flexibility is a liability here — it is exactly how an
  undocumented eighth edge type ends up in production data with no schema and no tests.
- **Generic traversal needs the `edge_all` view, and the view must be kept in sync.**
  A new typed table with no `UNION ALL` arm is invisible to generic traversal. Mitigation:
  a unit test enumerates tables matching the edge-table naming convention and asserts
  each appears in `edge_all`'s definition. Cheap, and it fails at CI time rather than as
  a quietly incomplete answer.
- **`UNION ALL` traversal is slower than one indexed table would be** for the rare
  all-types-at-once query. Acceptable: the hot path in retrieval is
  chunk → review_comment and chunk → commit → pull_request, both of which hit typed
  tables directly and never touch the view.

### Neutral

- Row counts stay small enough (Airflow: ~10^5 commits, ~10^5 discussion artefacts) that
  neither design has a storage problem. This decision is about correctness and query
  plans, not about scale.

## Alternatives considered

### 1. One polymorphic `edge` table

*Rejected.* No foreign keys, `jsonb` attributes, uniform `text` ids, and pooled
statistics. It is the design that looks cheapest on day one and is most expensive on
day sixty. The decisive argument is the foreign key: without it, nothing in the database
prevents a citation from pointing at a row that has been deleted, and the project's
central claim is that its citations are trustworthy.

### 2. A dedicated graph database (Neo4j, Memgraph)

*Rejected.* Traversals here are shallow — one to three hops, always from a known start
node — which is the regime where a relational join wins outright. Adopting a graph store
would also mean either running two databases (the vector and BM25 indexes must live in
Postgres alongside pgvector and pg_search) or synchronising them, which introduces a
consistency problem that does not currently exist. On a 2-core, 3.7 GB machine a second
JVM-backed database is also simply not affordable.

### 3. Postgres + Apache AGE (openCypher inside Postgres)

*Rejected.* Solves the "two databases" problem but not the "unvalidated attributes"
problem, adds an extension that does not ship in the pinned ParadeDB image, and would
have to coexist with pg_search and pgvector in a build we do not control. Pinning the
image by digest is load-bearing for reproducibility; adding an extension that forces a
custom image would trade a real invariant for a query-language convenience.

### 4. Adjacency lists in `jsonb` columns on the node tables

*Rejected.* Edge attributes have nowhere sensible to live, edges become one-directional
in practice, and every traversal turns into a `jsonb_array_elements` unnest that the
planner cannot use an index for. It also makes "which chunks does this review comment
touch?" — a query the system runs on every request — a full scan.

## Notes for the viva

The question this ADR anticipates is *"why not just one edges table? It's simpler."*
The honest answer is that it is simpler **to write** and harder **to be correct in**, and
the specific correctness property at stake — a citation always resolving to a live row —
is the one the whole project is judged on. The secondary answer is the query planner:
pooled statistics on a mixed-type table produce plans that are wrong by an order of
magnitude on hardware that has no margin to absorb it.
