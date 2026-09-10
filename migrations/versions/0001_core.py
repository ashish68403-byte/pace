"""0001_core: extensions, domains, enums, git object graph, chunks.

What this revision is for: the substrate every other revision hangs off --
repositories, ingest bookkeeping, the commit DAG, file revisions, and the
content-addressed chunk table with its lexical (bm25) index.

What failure it prevents:
  * Silent identifier drift. git_sha / sha256_hex are DOMAINS, not text. A
    truncated or upper-cased sha is rejected at write time by the database
    rather than discovered six weeks later as a join that returns zero rows.
  * Two ingesters racing on the same repo+driver (partial unique index on
    ingest_runs).
  * Losing failed subjects. Anything that blows up during ingest lands in
    dead_letters instead of being retried forever or dropped; the eval must be
    able to state exactly what was NOT ingested.
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_core"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Extensions.
    # pg_search installs the `paradedb` schema and the `pdb` type namespace
    # used by the bm25 index below. Verified on paradedb/paradedb, PG 18.6,
    # pgvector 0.8.4, pg_search 0.25.7.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE EXTENSION IF NOT EXISTS btree_gist;
        CREATE EXTENSION IF NOT EXISTS pg_search;
        """
    )

    # ------------------------------------------------------------------
    # Domains. CREATE DOMAIN has no IF NOT EXISTS, hence the DO blocks --
    # these keep `downgrade base && upgrade head` re-runnable even if a
    # previous downgrade was interrupted.
    # Lower-case hex only: git and hashlib both emit lower-case, so accepting
    # upper-case would create two spellings of the same key.
    # ------------------------------------------------------------------
    op.execute(
        """
        DO $$ BEGIN
            CREATE DOMAIN git_sha AS text
                CONSTRAINT git_sha_is_40_hex CHECK (VALUE ~ '^[0-9a-f]{40}$');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;

        DO $$ BEGIN
            CREATE DOMAIN sha256_hex AS text
                CONSTRAINT sha256_hex_is_64_hex CHECK (VALUE ~ '^[0-9a-f]{64}$');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )

    # ------------------------------------------------------------------
    # Enums. artifact_kind and edge_relation are the vocabulary of the
    # provenance graph; they are enums (not text) so a typo in a driver is a
    # write-time error, and so the provenance_edges view in 0003 has one
    # concrete type to union into.
    # ------------------------------------------------------------------
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE artifact_kind AS ENUM (
                'commit', 'file', 'file_revision', 'chunk', 'symbol',
                'pull_request', 'issue', 'issue_comment', 'review_comment'
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;

        DO $$ BEGIN
            CREATE TYPE edge_relation AS ENUM (
                'contains', 'modifies', 'introduces', 'reverts',
                'mentions', 'references', 'closes', 'fixes',
                'reviews', 'discusses', 'supersedes'
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;

        DO $$ BEGIN
            CREATE TYPE ingest_status AS ENUM (
                'pending', 'running', 'succeeded', 'failed', 'cancelled'
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;

        DO $$ BEGIN
            CREATE TYPE file_change AS ENUM (
                'added', 'modified', 'deleted', 'renamed', 'copied', 'type_changed'
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )

    # ------------------------------------------------------------------
    # repositories
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS repositories (
            repo_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            owner           text        NOT NULL,
            name            text        NOT NULL,
            default_branch  text        NOT NULL DEFAULT 'main',
            clone_path      text,
            head_sha        git_sha,
            created_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT repositories_owner_name_key UNIQUE (owner, name)
        );
        """
    )

    # ------------------------------------------------------------------
    # ingest_runs
    # rows_inserted / rows_updated / rows_deleted are the three counters the
    # idempotency gate reads: re-running a driver over an unchanged corpus
    # must produce inserted = updated = deleted = 0. That assertion is the
    # cheapest possible proof that ingest is deterministic, and it is a CI
    # gate, not a log line.
    # checkpoint is jsonb so each driver defines its own resume cursor (last
    # commit walked, last GitHub page + ETag, ...) without a migration.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_runs (
            run_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            repo_id          bigint        NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            driver           text          NOT NULL,
            driver_version   text          NOT NULL DEFAULT 'v1',
            status           ingest_status NOT NULL DEFAULT 'pending',
            started_at       timestamptz   NOT NULL DEFAULT now(),
            heartbeat_at     timestamptz   NOT NULL DEFAULT now(),
            finished_at      timestamptz,
            checkpoint       jsonb         NOT NULL DEFAULT '{}'::jsonb,
            last_indexed_sha git_sha,
            rows_inserted    bigint        NOT NULL DEFAULT 0,
            rows_updated     bigint        NOT NULL DEFAULT 0,
            rows_deleted     bigint        NOT NULL DEFAULT 0,
            rows_skipped     bigint        NOT NULL DEFAULT 0,
            trace_id         text,
            error_class      text,
            error_message    text,
            CONSTRAINT ingest_runs_finished_iff_terminal CHECK (
                (status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)
            )
        );

        -- At most one live run per (repo, driver). A plain unique constraint
        -- cannot express this: finished runs must be allowed to pile up.
        CREATE UNIQUE INDEX IF NOT EXISTS ingest_runs_one_running_per_driver
            ON ingest_runs (repo_id, driver)
            WHERE status = 'running';

        CREATE INDEX IF NOT EXISTS ingest_runs_repo_started_idx
            ON ingest_runs (repo_id, driver, started_at DESC);
        """
    )

    # ------------------------------------------------------------------
    # dead_letters
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dead_letters (
            dead_letter_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            repo_id        bigint      NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            run_id         bigint      REFERENCES ingest_runs(run_id) ON DELETE SET NULL,
            stage          text        NOT NULL,
            subject_ref    text        NOT NULL,
            commit_sha     git_sha,
            error_class    text        NOT NULL,
            error_message  text        NOT NULL,
            payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
            retry_count    integer     NOT NULL DEFAULT 0,
            first_seen_at  timestamptz NOT NULL DEFAULT now(),
            last_seen_at   timestamptz NOT NULL DEFAULT now(),
            resolved_at    timestamptz
        );

        -- One row per failing subject, not one per retry: re-failing bumps
        -- retry_count via ON CONFLICT. commit_sha is nullable and NULLs are
        -- distinct in a unique index, so it is coalesced to '' -- otherwise
        -- every retry of a commit-less subject inserts a fresh duplicate.
        -- The ::text cast is required: '' does not satisfy the git_sha domain.
        CREATE UNIQUE INDEX IF NOT EXISTS dead_letters_subject_key
            ON dead_letters (repo_id, stage, subject_ref, (coalesce(commit_sha::text, '')));

        CREATE INDEX IF NOT EXISTS dead_letters_unresolved_idx
            ON dead_letters (repo_id, stage, last_seen_at DESC)
            WHERE resolved_at IS NULL;
        """
    )

    # ------------------------------------------------------------------
    # commits
    # PK is (repo_id, commit_sha) rather than a surrogate id: every downstream
    # typed edge table then carries a real composite FK to a *named* commit,
    # so a link to a commit that was never ingested cannot be written.
    # topo_order is assigned once by the git walker and is the sort key that
    # makes symbol_versions (0002) a clustered range scan.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS commits (
            repo_id         bigint      NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            commit_sha      git_sha     NOT NULL,
            tree_sha        git_sha,
            topo_order      bigint      NOT NULL,
            author_name     text,
            author_email    text,
            authored_at     timestamptz NOT NULL,
            committer_name  text,
            committer_email text,
            committed_at    timestamptz NOT NULL,
            subject         text        NOT NULL DEFAULT '',
            message         text        NOT NULL DEFAULT '',
            insertions      integer     NOT NULL DEFAULT 0,
            deletions       integer     NOT NULL DEFAULT 0,
            files_changed   integer     NOT NULL DEFAULT 0,
            is_merge        boolean     NOT NULL DEFAULT false,
            -- is_mechanical marks reformat / lint / codemod / license-header
            -- commits. Measured on apache/airflow: ~29% of blamed lines
            -- resolve to a mechanical reformat commit (the black rollout being
            -- the worst offender). Blame that stops there answers "who ran
            -- black", not "why is this code the way it is", so the lineage
            -- walker in 0002 must be able to skip these rows. It is a stored
            -- column and not a query-time filter because the classifier is
            -- versioned and its verdict has to stay auditable.
            is_mechanical   boolean     NOT NULL DEFAULT false,
            mechanical_reason text,
            ingested_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (repo_id, commit_sha),
            CONSTRAINT commits_topo_unique UNIQUE (repo_id, topo_order)
        );

        CREATE INDEX IF NOT EXISTS commits_authored_at_idx
            ON commits (repo_id, authored_at DESC);

        CREATE INDEX IF NOT EXISTS commits_substantive_idx
            ON commits (repo_id, topo_order)
            WHERE NOT is_mechanical AND NOT is_merge;

        -- Trigram index over the subject line: the regex link recovery in 0003
        -- ("Closes #1234", "Revert of abcd123") scans these.
        CREATE INDEX IF NOT EXISTS commits_subject_trgm_idx
            ON commits USING gin (subject gin_trgm_ops);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS commit_parents (
            repo_id      bigint   NOT NULL,
            child_sha    git_sha  NOT NULL,
            parent_index smallint NOT NULL,
            parent_sha   git_sha  NOT NULL,
            PRIMARY KEY (repo_id, child_sha, parent_index),
            CONSTRAINT commit_parents_child_fk
                FOREIGN KEY (repo_id, child_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT commit_parents_parent_fk
                FOREIGN KEY (repo_id, parent_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT commit_parents_index_nonneg CHECK (parent_index >= 0)
        );

        -- Walk the DAG downwards (children of a commit) without a seq scan.
        CREATE INDEX IF NOT EXISTS commit_parents_parent_idx
            ON commit_parents (repo_id, parent_sha);
        """
    )

    # ------------------------------------------------------------------
    # files / file_revisions
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            file_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            repo_id           bigint  NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            path              text    NOT NULL,
            language          text,
            is_binary         boolean NOT NULL DEFAULT false,
            is_generated      boolean NOT NULL DEFAULT false,
            first_seen_commit git_sha,
            last_seen_commit  git_sha,
            deleted_at_commit git_sha,
            CONSTRAINT files_repo_path_key UNIQUE (repo_id, path)
        );

        CREATE INDEX IF NOT EXISTS files_path_trgm_idx
            ON files USING gin (path gin_trgm_ops);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS file_revisions (
            file_revision_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            repo_id           bigint      NOT NULL,
            file_id           bigint      NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
            commit_sha        git_sha     NOT NULL,
            commit_topo_order bigint      NOT NULL,
            blob_sha          git_sha     NOT NULL,
            change_type       file_change NOT NULL,
            old_path          text,
            insertions        integer     NOT NULL DEFAULT 0,
            deletions         integer     NOT NULL DEFAULT 0,
            similarity        smallint,
            CONSTRAINT file_revisions_commit_fk
                FOREIGN KEY (repo_id, commit_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT file_revisions_file_commit_key UNIQUE (file_id, commit_sha),
            CONSTRAINT file_revisions_rename_has_old_path CHECK (
                change_type <> 'renamed' OR old_path IS NOT NULL
            )
        );

        -- blob_sha is the parse-cache key. Airflow re-touches the same blob
        -- across thousands of commits (merges, cherry-picks, reverts); keying
        -- the tree-sitter cache on blob content instead of (path, commit) is
        -- what makes a full-history parse finish on 2 cores.
        CREATE INDEX IF NOT EXISTS file_revisions_blob_sha_idx
            ON file_revisions (blob_sha);

        CREATE INDEX IF NOT EXISTS file_revisions_file_topo_idx
            ON file_revisions (file_id, commit_topo_order DESC);
        """
    )

    # ------------------------------------------------------------------
    # chunks
    # chunk_id is content-addressed (see provenance/graph/ids.py) and is the
    # PK. chunking_strategy_version is a COLUMN, never an input to the hash:
    # folding it in would invalidate every chunk_id -- and therefore every
    # golden-set anchor -- the first time the chunker changes.
    # tombstoned_at_commit keeps deleted code queryable: "why is this the way
    # it is" frequently means "why was this removed", and a hard DELETE makes
    # that question unanswerable.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id             sha256_hex    NOT NULL PRIMARY KEY,
            -- Compact surrogate used ONLY as the bm25 key_field (below).
            -- pg_search wants a small stable key; the 64-char content hash
            -- stays the real identity.
            chunk_row_id         bigint        GENERATED ALWAYS AS IDENTITY NOT NULL UNIQUE,
            repo_id              bigint        NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            file_id              bigint        REFERENCES files(file_id) ON DELETE SET NULL,
            path                 text          NOT NULL,
            qualified_name       text,
            kind                 artifact_kind NOT NULL DEFAULT 'chunk',
            language             text          NOT NULL DEFAULT 'python',
            start_line           integer       NOT NULL,
            end_line             integer       NOT NULL,
            content              text          NOT NULL,
            content_sha          sha256_hex    NOT NULL,
            -- Provenance of derivation, not identity. See ids.py.
            chunking_strategy_version text     NOT NULL,
            -- Tokens the pdb.source_code tokenizer cannot produce: the
            -- underscore-free joined form ('_normalise_url' -> 'normaliseurl')
            -- plus dotted qualified names. Verified: the whole identifier does
            -- NOT survive tokenization, only its parts.
            lexical_tokens       text[]        NOT NULL DEFAULT '{}',
            -- Space-joined form of lexical_tokens, populated by the ingester.
            -- NOT a generated column: array_to_string() is not immutable, so
            -- Postgres rejects it in a GENERATED ... STORED expression
            -- (verified: 'generation expression is not immutable').
            lexical_blob         text          NOT NULL DEFAULT '',
            visibility_group     text          NOT NULL DEFAULT 'public',
            first_seen_commit    git_sha       NOT NULL,
            last_seen_commit     git_sha       NOT NULL,
            tombstoned_at_commit git_sha,
            created_at           timestamptz   NOT NULL DEFAULT now(),
            CONSTRAINT chunks_line_span CHECK (end_line >= start_line AND start_line >= 1)
        );

        CREATE INDEX IF NOT EXISTS chunks_file_idx  ON chunks (file_id, start_line);
        CREATE INDEX IF NOT EXISTS chunks_qname_idx ON chunks (repo_id, path, qualified_name);
        CREATE INDEX IF NOT EXISTS chunks_lexical_tokens_idx
            ON chunks USING gin (lexical_tokens);

        -- Live (non-tombstoned) chunks are the default retrieval surface;
        -- archaeology explicitly opts into the tombstoned ones.
        CREATE INDEX IF NOT EXISTS chunks_live_idx
            ON chunks (repo_id, path)
            WHERE tombstoned_at_commit IS NULL;
        """
    )

    # ------------------------------------------------------------------
    # bm25 index.
    #
    # MEASURED CONSTRAINTS (paradedb/paradedb, PG 18.6, pg_search 0.25.7).
    # All three were hit for real; none are documented prominently upstream:
    #
    #   1. The same column may NOT appear twice under two casts:
    #        CREATE INDEX ... USING bm25 (k, (content::pdb.source_code),
    #                                        (content::pdb.literal))
    #      -> ERROR: indexed attribute content defined more than once
    #
    #   2. A table may have only ONE ParadeDB index:
    #        -> ERROR: a relation may only have one ParadeDB index
    #      So "one index per tokenizer" is not available either.
    #
    #   3. pdb.literal emits the ENTIRE field as a single token:
    #        'def _normalise_url(u)'::pdb.literal -> {"def _normalise_url(u)"}
    #      so casting a whole content column to literal is useless -- every
    #      document becomes one enormous term.
    #
    # Therefore: one index, over THREE DISTINCT columns, each with the
    # tokenizer that suits its shape.
    #
    #   content::pdb.source_code    -> split tokens, for natural code search
    #       'let my_variable = 2;'         -> {let, my, variable, 2}
    #       '_normalise_url camelCaseName' -> {normalise, url, camel, case, name}
    #   qualified_name::pdb.literal -> the exact dotted symbol name. Short
    #       field, so one-token-per-field is exactly what we want here:
    #       'airflow.utils.helpers._normalise_url' matches as a unit.
    #   lexical_blob::pdb.source_code -> the augmented token bag, carrying the
    #       underscore-free joined form ('_normalise_url' -> 'normaliseurl')
    #       that source_code tokenization destroys and that a user searching
    #       for a bare identifier will type.
    #
    # Verified working: split match, exact qualified-name match, joined-form
    # match, cross-field OR, and real BM25 scoring via paradedb.score().
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS chunks_bm25_idx ON chunks
        USING bm25 (
            chunk_row_id,
            (content::pdb.source_code),
            (qualified_name::pdb.literal),
            (lexical_blob::pdb.source_code)
        )
        WITH (key_field = 'chunk_row_id');
        """
    )
    # Fallback if pg_search is unavailable (a plain postgres:18 image in CI) or
    # if a paradedb build rejects the same column under two pdb casts. Strictly
    # worse -- no BM25 scoring, no phrase queries -- but it keeps lexical
    # retrieval running so the eval harness still measures something:
    #
    #   CREATE INDEX chunks_content_trgm_idx
    #       ON chunks USING gin (content gin_trgm_ops);
    #   ALTER TABLE chunks
    #       ADD COLUMN content_tsv tsvector
    #       GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED;
    #   CREATE INDEX chunks_content_tsv_idx ON chunks USING gin (content_tsv);


def downgrade() -> None:
    # Drop in reverse dependency order. Domains and enums go too, so
    # `downgrade base` really does return the database to empty and the second
    # `upgrade head` exercises the CREATE path, not the IF NOT EXISTS path.
    op.execute(
        """
        DROP TABLE IF EXISTS chunks         CASCADE;
        DROP TABLE IF EXISTS file_revisions CASCADE;
        DROP TABLE IF EXISTS files          CASCADE;
        DROP TABLE IF EXISTS commit_parents CASCADE;
        DROP TABLE IF EXISTS commits        CASCADE;
        DROP TABLE IF EXISTS dead_letters   CASCADE;
        DROP TABLE IF EXISTS ingest_runs    CASCADE;
        DROP TABLE IF EXISTS repositories   CASCADE;

        DROP TYPE   IF EXISTS file_change;
        DROP TYPE   IF EXISTS ingest_status;
        DROP TYPE   IF EXISTS edge_relation;
        DROP TYPE   IF EXISTS artifact_kind;

        DROP DOMAIN IF EXISTS sha256_hex;
        DROP DOMAIN IF EXISTS git_sha;
        """
    )
    # Extensions are deliberately NOT dropped. They are database-scoped and may
    # be in use by anything else in this database; DROP EXTENSION vector CASCADE
    # would silently destroy unrelated columns. Removing them is an operator
    # decision, not a migration's.
