"""0002_lineage: symbol identity through time, typed lineage DAG, blame ranges.

What this revision is for: answering "this function, all the way back" without
the answer degenerating into "git blame said black touched it in 2020".

  symbol_lineage  -- one row per symbol *identity* (survives rename and move).
  symbol_versions -- one row per (identity, commit); the clustered spine.
  lineage_edges   -- the N:M events a single chain cannot express.
  blame_ranges    -- line ownership at a snapshot, non-overlapping by construction.

What failure it prevents:
  * A chain-shaped lineage model. extract-method and inline-method are N:M
    (one symbol becomes three, three collapse into one). A parent pointer on
    symbol_versions can encode neither, so the walker would silently pick one
    arbitrary survivor and drop the rest of the history.
  * Comparing hashes across Python versions. ast.dump output is not stable
    across CPython minors -- field order and repr details change -- so an h2
    computed on 3.12 and one computed on 3.13 are different strings for
    identical code. ast_norm_version records the exact interpreter/grammar so
    a mismatched pair is detected instead of scored as "the code changed".
  * Overlapping blame. Two ranges claiming line 40 of the same snapshot make
    citation non-deterministic; the GiST exclusion constraint makes that state
    unrepresentable rather than merely unlikely.
"""

from __future__ import annotations

from alembic import op

revision: str = "0002_lineage"
down_revision: str | None = "0001_core"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE lineage_edge_kind AS ENUM (
                'extract', 'inline', 'split', 'merge', 'ambiguous'
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )

    # ------------------------------------------------------------------
    # symbol_lineage: the durable identity. Nothing here is derived from the
    # current path or name -- both change -- so downstream tables key on
    # lineage_id and the golden set keys on (path, qualified_name), which
    # resolve_anchors() maps forward at scoring time.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_lineage (
            lineage_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            repo_id                bigint  NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            symbol_kind            text    NOT NULL,
            language               text    NOT NULL DEFAULT 'python',
            birth_commit           git_sha NOT NULL,
            birth_topo_order       bigint  NOT NULL,
            death_commit           git_sha,
            death_topo_order       bigint,
            current_path           text,
            current_qualified_name text,
            version_count          integer NOT NULL DEFAULT 0,
            detector_version       text    NOT NULL DEFAULT 'v1',
            CONSTRAINT symbol_lineage_birth_fk
                FOREIGN KEY (repo_id, birth_commit)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT symbol_lineage_death_ordered CHECK (
                death_topo_order IS NULL OR death_topo_order >= birth_topo_order
            ),
            -- Needed so symbol_versions can carry a composite FK that pins a
            -- version to a lineage in the same repo.
            CONSTRAINT symbol_lineage_repo_key UNIQUE (repo_id, lineage_id)
        );

        CREATE INDEX IF NOT EXISTS symbol_lineage_current_idx
            ON symbol_lineage (repo_id, current_path, current_qualified_name)
            WHERE death_commit IS NULL;
        """
    )

    # ------------------------------------------------------------------
    # symbol_versions
    #
    # PRIMARY KEY (lineage_id, commit_topo_order) -- note the order. The whole
    # history of one symbol is then a single contiguous range in the PK btree,
    # so "walk this function back through 400 commits" is one index range scan
    # instead of 400 scattered heap lookups. Adding a surrogate id and a
    # separate index would cost an extra random read per version; on 3.7 GB of
    # WSL RAM that is the difference between cached and not.
    #
    # The hash ladder h0..h3 is a cascade of decreasing strictness. Matching
    # stops at the first level that yields exactly one candidate:
    #   h0 -- sha256 of normalized source. Byte-identical body.
    #   h1 -- comments and docstrings stripped, whitespace collapsed.
    #         Survives reformatting; this is what defeats the black rollout.
    #   h2 -- AST structure with identifiers/literals alpha-renamed to
    #         positional placeholders. Survives renaming.
    #   h3 -- shape only: node-type histogram + arity + control-flow skeleton.
    #         Deliberately lossy; only ever used to *propose* a match, which is
    #         why match_stage and match_score are on every row.
    # match_stage/match_score are NOT NULL on every row -- including exact
    # matches, which record ('h0', 1.0). A nullable confidence would let an
    # unmeasured guess look identical to a certainty in every downstream join.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_versions (
            lineage_id        bigint     NOT NULL,
            commit_topo_order bigint     NOT NULL,
            repo_id           bigint     NOT NULL,
            commit_sha        git_sha    NOT NULL,
            path              text       NOT NULL,
            qualified_name    text       NOT NULL,
            signature         text,
            start_line        integer    NOT NULL,
            end_line          integer    NOT NULL,
            chunk_id          sha256_hex REFERENCES chunks(chunk_id) ON DELETE SET NULL,
            h0                sha256_hex NOT NULL,
            h1                sha256_hex NOT NULL,
            h2                sha256_hex NOT NULL,
            h3                sha256_hex NOT NULL,
            -- Exact interpreter + grammar that produced h1..h3, e.g.
            -- 'cpython-3.12.3/ts-python-0.23.6/norm-v1'. ast.dump is not
            -- stable across Python minors: comparing an h2 from 3.12 with one
            -- from 3.13 is comparing two different functions of the same code.
            ast_norm_version  text       NOT NULL,
            match_stage       text       NOT NULL,
            match_score       real       NOT NULL,
            is_rename         boolean    NOT NULL DEFAULT false,
            is_move           boolean    NOT NULL DEFAULT false,
            is_body_change    boolean    NOT NULL DEFAULT false,
            PRIMARY KEY (lineage_id, commit_topo_order),
            CONSTRAINT symbol_versions_lineage_fk
                FOREIGN KEY (repo_id, lineage_id)
                REFERENCES symbol_lineage(repo_id, lineage_id) ON DELETE CASCADE,
            CONSTRAINT symbol_versions_commit_fk
                FOREIGN KEY (repo_id, commit_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT symbol_versions_stage_known CHECK (
                match_stage IN ('h0', 'h1', 'h2', 'h3', 'rename_detect', 'manual')
            ),
            CONSTRAINT symbol_versions_score_range CHECK (match_score >= 0.0 AND match_score <= 1.0),
            CONSTRAINT symbol_versions_line_span CHECK (end_line >= start_line AND start_line >= 1)
        );

        -- "which symbols live at this commit" -- the reverse of the PK order.
        CREATE INDEX IF NOT EXISTS symbol_versions_commit_idx
            ON symbol_versions (repo_id, commit_sha);

        -- Ladder lookups: given a hash, which lineages already have it.
        CREATE INDEX IF NOT EXISTS symbol_versions_h1_idx ON symbol_versions (h1);
        CREATE INDEX IF NOT EXISTS symbol_versions_h2_idx ON symbol_versions (h2);

        CREATE INDEX IF NOT EXISTS symbol_versions_qname_idx
            ON symbol_versions (repo_id, path, qualified_name, commit_topo_order DESC);

        -- Everything matched below h1 is a judgement call and must be
        -- reviewable in bulk during the eval writeup.
        CREATE INDEX IF NOT EXISTS symbol_versions_uncertain_idx
            ON symbol_versions (repo_id, match_score)
            WHERE match_stage IN ('h2', 'h3', 'rename_detect');
        """
    )

    # ------------------------------------------------------------------
    # lineage_edges: a typed DAG *between* identities, not inside one.
    # extract and inline are N:M -- one function becomes three, or three
    # collapse into one -- and a parent pointer on symbol_versions cannot say
    # "these three came from that one" without picking a winner and losing the
    # rest. confidence is per edge because a split detected by exact body
    # containment and one detected by h3 shape similarity are not the same
    # claim, and the answer must be able to say which it is standing on.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS lineage_edges (
            lineage_edge_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            repo_id          bigint            NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            kind             lineage_edge_kind NOT NULL,
            src_lineage_id   bigint            NOT NULL REFERENCES symbol_lineage(lineage_id) ON DELETE CASCADE,
            dst_lineage_id   bigint            NOT NULL REFERENCES symbol_lineage(lineage_id) ON DELETE CASCADE,
            at_commit        git_sha           NOT NULL,
            at_topo_order    bigint            NOT NULL,
            confidence       real              NOT NULL,
            detector         text              NOT NULL,
            detector_version text              NOT NULL DEFAULT 'v1',
            evidence         jsonb             NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT lineage_edges_commit_fk
                FOREIGN KEY (repo_id, at_commit)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT lineage_edges_confidence_range CHECK (confidence >= 0.0 AND confidence <= 1.0),
            CONSTRAINT lineage_edges_no_self_loop CHECK (src_lineage_id <> dst_lineage_id),
            CONSTRAINT lineage_edges_unique
                UNIQUE (src_lineage_id, dst_lineage_id, kind, at_commit, detector)
        );

        CREATE INDEX IF NOT EXISTS lineage_edges_src_idx
            ON lineage_edges (src_lineage_id, at_topo_order);
        CREATE INDEX IF NOT EXISTS lineage_edges_dst_idx
            ON lineage_edges (dst_lineage_id, at_topo_order);

        -- 'ambiguous' edges are the honest output of a detector that found two
        -- equally good candidates. They are kept (refusal needs evidence) but
        -- must never be traversed silently, so they get their own index and
        -- the walker has to opt in.
        CREATE INDEX IF NOT EXISTS lineage_edges_ambiguous_idx
            ON lineage_edges (repo_id, at_topo_order)
            WHERE kind = 'ambiguous';
        """
    )

    # ------------------------------------------------------------------
    # blame_ranges
    # line_range is int4range, half-open [start, end): line 40 alone is
    # int4range(40, 41). Storing a range rather than (start_line, end_line)
    # buys the && operator, and && plus btree_gist buys the exclusion
    # constraint below.
    #
    # The EXCLUDE constraint is the point of this table: for one
    # (file_id, snapshot_sha) no two rows may overlap. Blame that assigns line
    # 40 to two different origin commits makes every citation built on it
    # non-deterministic. snapshot_sha is cast to text in the constraint because
    # it is a domain and the btree_gist operator class is registered against
    # the base type.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS blame_ranges (
            blame_range_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            repo_id          bigint     NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            file_id          bigint     NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
            snapshot_sha     git_sha    NOT NULL,
            line_range       int4range  NOT NULL,
            orig_commit_sha  git_sha    NOT NULL,
            orig_path        text       NOT NULL,
            orig_line_start  integer    NOT NULL,
            -- The first non-mechanical ancestor of orig_commit_sha, resolved
            -- once at ingest. ~29% of blamed lines in airflow land on a
            -- reformat commit; this column is what the citation actually
            -- shows, with orig_commit_sha kept for auditability.
            effective_commit_sha git_sha,
            chunk_id         sha256_hex REFERENCES chunks(chunk_id) ON DELETE SET NULL,
            blame_algo       text       NOT NULL DEFAULT 'pygit2-default',
            computed_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT blame_ranges_snapshot_fk
                FOREIGN KEY (repo_id, snapshot_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT blame_ranges_orig_fk
                FOREIGN KEY (repo_id, orig_commit_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT blame_ranges_nonempty CHECK (NOT isempty(line_range)),
            CONSTRAINT blame_ranges_positive CHECK (lower(line_range) >= 1),
            CONSTRAINT blame_ranges_no_overlap EXCLUDE USING gist (
                file_id WITH =,
                (snapshot_sha::text) WITH =,
                line_range WITH &&
            )
        );

        CREATE INDEX IF NOT EXISTS blame_ranges_orig_idx
            ON blame_ranges (repo_id, orig_commit_sha);
        CREATE INDEX IF NOT EXISTS blame_ranges_chunk_idx
            ON blame_ranges (chunk_id)
            WHERE chunk_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS blame_ranges    CASCADE;
        DROP TABLE IF EXISTS lineage_edges   CASCADE;
        DROP TABLE IF EXISTS symbol_versions CASCADE;
        DROP TABLE IF EXISTS symbol_lineage  CASCADE;

        DROP TYPE IF EXISTS lineage_edge_kind;
        """
    )
