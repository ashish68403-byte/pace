"""0004_vectors: embedding model registry, blue/green index aliases, vector tables.

What this revision is for: dense retrieval that can be rebuilt from scratch
without downtime and without lying about which model produced which vector.

What failure it prevents:
  * Mixed-model neighbourhoods. Vectors from two models in one table produce
    cosine scores that are arithmetically valid and semantically meaningless.
    model_version is a NOT NULL FK into embedding_models and part of every
    primary key, so a partial re-embed cannot quietly interleave.
  * A rebuild that takes the index offline. Readers resolve a logical alias
    ('code_current') to a physical table; the rebuild fills the idle colour and
    the flip is two UPDATEs in one transaction.
  * A post-filter masquerading as authz. See the partial HNSW index below.

Dimension note: vector(384) is hardcoded because DDL must be deterministic --
a migration that reads settings would produce a different schema depending on
the environment it ran in. It must agree with settings.embedding_dim (384,
bge-small-en-v1.5); embedding_models.dim records it so a mismatch is a
detectable row, not a runtime cast error.
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_vectors"
down_revision: str | None = "0003_github"
branch_labels: str | None = None
depends_on: str | None = None

_DIM = 384


def upgrade() -> None:
    # ------------------------------------------------------------------
    # embedding_models: the registry. `normalize` matters -- if vectors are
    # L2-normalized then cosine and inner product rank identically and the
    # cheaper operator can be used; if they are not, using the wrong one
    # silently reorders results.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_models (
            model_version text        NOT NULL PRIMARY KEY,
            provider      text        NOT NULL,
            dim           integer     NOT NULL,
            normalize     boolean     NOT NULL DEFAULT true,
            pooling       text        NOT NULL DEFAULT 'cls',
            max_tokens    integer     NOT NULL DEFAULT 512,
            revision      text,
            notes         text,
            created_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT embedding_models_dim_positive CHECK (dim > 0)
        );
        """
    )

    # ------------------------------------------------------------------
    # index_aliases
    #
    # physical_table is the PRIMARY KEY and alias is a NULLABLE UNIQUE column.
    # The reverse -- alias as PK -- breaks the blue/green flip. The flip is:
    #
    #     BEGIN;
    #     UPDATE index_aliases SET alias = NULL
    #      WHERE alias = 'code_current';
    #     UPDATE index_aliases SET alias = 'code_current'
    #      WHERE physical_table = 'embeddings_code_green';
    #     COMMIT;
    #
    # With alias as the PK there is a moment where both rows would need to be
    # 'code_current', or the old row must be DELETEd and re-INSERTed, losing
    # its build metadata. Unique indexes are enforced per statement, so
    # clearing to NULL first and then setting is legal here; NULLs are distinct
    # in a unique index, which is exactly why alias must be nullable.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS index_aliases (
            physical_table text        NOT NULL PRIMARY KEY,
            alias          text        UNIQUE,
            content_domain text        NOT NULL,
            model_version  text        NOT NULL REFERENCES embedding_models(model_version),
            colour         text        NOT NULL,
            row_count      bigint      NOT NULL DEFAULT 0,
            is_ready       boolean     NOT NULL DEFAULT false,
            built_at       timestamptz,
            promoted_at    timestamptz,
            CONSTRAINT index_aliases_domain_known CHECK (content_domain IN ('code', 'prose')),
            CONSTRAINT index_aliases_colour_known CHECK (colour IN ('blue', 'green')),
            -- A table can only be promoted once it has actually been built.
            CONSTRAINT index_aliases_alias_requires_ready CHECK (alias IS NULL OR is_ready)
        );
        """
    )

    # ------------------------------------------------------------------
    # embeddings_code_blue -- the template. Green is created LIKE it below.
    #
    # The embedding column is full-precision vector(384). Brute-force k-NN over
    # this column is the ground truth the HNSW recall@k number is measured
    # against; store halfvec and the "ground truth" is itself approximate and
    # the recall figure means nothing.
    #
    # The HNSW index is built on a halfvec CAST of that column: half precision
    # roughly halves index size and build time (on 2 cores and ~3.7 GB of WSL
    # RAM that is the difference between a build that finishes and one that
    # swaps), while the exact vector stays on the heap for rescoring.
    # Queries must cast the probe the same way or the index is not used:
    #     ORDER BY embedding::halfvec(384) <=> $1::halfvec(384)
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS embeddings_code_blue (
            chunk_id         sha256_hex  NOT NULL,
            model_version    text        NOT NULL,
            embedding        vector({_DIM}) NOT NULL,
            -- Denormalized from chunks so the pre-filter below never has to
            -- join to evaluate authz.
            visibility_group text        NOT NULL DEFAULT 'public',
            content_sha      sha256_hex  NOT NULL,
            embedded_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (chunk_id, model_version)
        );

        CREATE INDEX IF NOT EXISTS embeddings_code_blue_hnsw
            ON embeddings_code_blue
            USING hnsw ((embedding::halfvec({_DIM})) halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """
    )

    # ------------------------------------------------------------------
    # The genuine pre-filter.
    #
    # pgvector's HNSW is a POST-filter: `ORDER BY ... LIMIT 10 WHERE
    # visibility_group = 'internal'` searches the graph over ALL vectors, takes
    # the ef_search candidates it finds, and only then discards the ones that
    # fail the WHERE. If internal chunks are 2% of the corpus, most queries
    # return fewer than 10 rows -- or zero -- while the planner reports a
    # perfectly healthy index scan. Worse, it means the traversal walked
    # vectors the caller is not allowed to see; that is a timing side channel,
    # not just a recall bug.
    #
    # A PARTIAL index scoped to one visibility group is a real pre-filter: the
    # graph itself contains only permitted vectors, so recall is honest and
    # nothing else is ever touched. It costs one index per group, which is why
    # groups must stay few and coarse. The authz chapter turns on this
    # distinction.
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS embeddings_code_blue_hnsw_public
            ON embeddings_code_blue
            USING hnsw ((embedding::halfvec({_DIM})) halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE visibility_group = 'public';
        """
    )

    # FKs added explicitly, BEFORE the LIKE, so the LIKE below demonstrably
    # does not copy them.
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE embeddings_code_blue
            ADD CONSTRAINT embeddings_code_blue_chunk_fk
            FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;

        DO $$ BEGIN
            ALTER TABLE embeddings_code_blue
            ADD CONSTRAINT embeddings_code_blue_model_fk
            FOREIGN KEY (model_version) REFERENCES embedding_models(model_version);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;

        """
    )

    # ------------------------------------------------------------------
    # Green.
    # CREATE TABLE ... LIKE ... INCLUDING ALL copies columns, defaults, NOT
    # NULLs, CHECK constraints, the primary key, indexes (including the two
    # expression HNSW indexes and the partial one) and comments. It does NOT
    # copy FOREIGN KEYS -- there is no INCLUDING FOREIGN KEYS option in
    # Postgres, at any version. Forget this and the idle colour accepts
    # embeddings for chunk_ids that no longer exist, and the flip promotes a
    # table full of dangling rows.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings_code_green
            (LIKE embeddings_code_blue INCLUDING ALL);
        DO $$ BEGIN
            ALTER TABLE embeddings_code_green
            ADD CONSTRAINT embeddings_code_green_chunk_fk
            FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;

        DO $$ BEGIN
            ALTER TABLE embeddings_code_green
            ADD CONSTRAINT embeddings_code_green_model_fk
            FOREIGN KEY (model_version) REFERENCES embedding_models(model_version);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;

        """
    )

    # ------------------------------------------------------------------
    # Prose embeddings: PR bodies, issue bodies, review comments.
    # Keyed on doc_ref ('pull_request:12345', 'review_comment:987') rather than
    # chunk_id -- prose is not code and has no chunk row, and forcing it into
    # the chunks table would put English into the bm25 source_code tokenizer.
    # No FK to chunks for the same reason; doc_ref shape is enforced by CHECK.
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS embeddings_prose_blue (
            doc_ref          text        NOT NULL,
            model_version    text        NOT NULL,
            embedding        vector({_DIM}) NOT NULL,
            visibility_group text        NOT NULL DEFAULT 'public',
            content_sha      sha256_hex  NOT NULL,
            embedded_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (doc_ref, model_version),
            CONSTRAINT embeddings_prose_blue_doc_ref_shape CHECK (
                doc_ref ~ '^(pull_request|issue|issue_comment|review_comment|commit_message):[A-Za-z0-9_-]+$'
            )
        );

        CREATE INDEX IF NOT EXISTS embeddings_prose_blue_hnsw
            ON embeddings_prose_blue
            USING hnsw ((embedding::halfvec({_DIM})) halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64);

        CREATE INDEX IF NOT EXISTS embeddings_prose_blue_hnsw_public
            ON embeddings_prose_blue
            USING hnsw ((embedding::halfvec({_DIM})) halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE visibility_group = 'public';
        """
    )

    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE embeddings_prose_blue
            ADD CONSTRAINT embeddings_prose_blue_model_fk
            FOREIGN KEY (model_version) REFERENCES embedding_models(model_version);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;


        CREATE TABLE IF NOT EXISTS embeddings_prose_green
            (LIKE embeddings_prose_blue INCLUDING ALL);

        -- Again: INCLUDING ALL brought the CHECK and the indexes, not this.
        DO $$ BEGIN
            ALTER TABLE embeddings_prose_green
            ADD CONSTRAINT embeddings_prose_green_model_fk
            FOREIGN KEY (model_version) REFERENCES embedding_models(model_version);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;

        """
    )

    # ------------------------------------------------------------------
    # Seed the registry and the aliases. Blue starts live-but-empty so the
    # walking skeleton has something to resolve; green is the idle rebuild
    # target with alias NULL.
    # ------------------------------------------------------------------
    op.execute(
        f"""
        INSERT INTO embedding_models (model_version, provider, dim, normalize, pooling, max_tokens, notes)
        VALUES ('bge-small-en-v1.5', 'sentence-transformers', {_DIM}, true, 'cls', 512,
                'Walking-skeleton model. CPU-only: no torch in the base dependency set, '
                'see the optional "ml" extra.')
        ON CONFLICT (model_version) DO NOTHING;

        INSERT INTO index_aliases (physical_table, alias, content_domain, model_version, colour, is_ready, built_at)
        VALUES
            ('embeddings_code_blue',   'code_current',  'code',  'bge-small-en-v1.5', 'blue',  true, now()),
            ('embeddings_code_green',   NULL,           'code',  'bge-small-en-v1.5', 'green', false, NULL),
            ('embeddings_prose_blue',  'prose_current', 'prose', 'bge-small-en-v1.5', 'blue',  true, now()),
            ('embeddings_prose_green',  NULL,           'prose', 'bge-small-en-v1.5', 'green', false, NULL)
        ON CONFLICT (physical_table) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS embeddings_prose_green CASCADE;
        DROP TABLE IF EXISTS embeddings_prose_blue  CASCADE;
        DROP TABLE IF EXISTS embeddings_code_green  CASCADE;
        DROP TABLE IF EXISTS embeddings_code_blue   CASCADE;
        DROP TABLE IF EXISTS index_aliases          CASCADE;
        DROP TABLE IF EXISTS embedding_models       CASCADE;
        """
    )
