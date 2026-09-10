"""chunk_evidence: the link that lets gold survive a re-chunk.

Revision ID: 0005_chunk_evidence
Revises: 0004_vectors

The golden set keys its gold on commit SHAs, PR numbers, issue numbers and
review-comment ids -- deliberately never on chunk_ids, because a chunk_id is a
hash of normalized content and therefore changes the first time the chunker is
tuned. Scoring, though, compares against retrieved chunks. Something has to map
"the rationale lives in PR 47798" onto "these chunk_ids, as of the current
chunking strategy", at scoring time rather than at generation time.

That mapping is this table, and it was missing: `resolve_anchors()` is the only
path from gold evidence to chunks, and it queried a relation no migration
created. Without it the eval harness cannot score anything, and the project's
central evaluation claim -- verifiable ID-level ground truth that outlives the
pipeline that produced it -- is unimplementable.

Rows are derived, not authored. The ingester rebuilds them; nothing here is
hand-maintained, which is why the whole table is safe to drop and recompute.
"""

from __future__ import annotations

from alembic import op

revision: str = "0005_chunk_evidence"
down_revision: str | None = "0004_vectors"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chunk_evidence (
            repo_id       bigint        NOT NULL
                              REFERENCES repositories(repo_id) ON DELETE CASCADE,
            chunk_id      sha256_hex    NOT NULL
                              REFERENCES chunks(chunk_id) ON DELETE CASCADE,
            -- What kind of upstream artifact this chunk is evidence for.
            evidence_kind artifact_kind NOT NULL,
            -- The natural upstream key, as text so one column serves all kinds:
            -- a 40-hex sha, a PR number, an issue number, a review comment id.
            evidence_key  text          NOT NULL,
            -- How the link was established. A closed set, like every other
            -- `source` column in this schema, so `GROUP BY source` reports
            -- honestly what fraction of the graph came from structural events
            -- rather than heuristics.
            source        text          NOT NULL
                              CHECK (source IN ('blame','ast_identity','diff',
                                                'lineage_edge','manual')),
            confidence    real          NOT NULL DEFAULT 1.0
                              CHECK (confidence BETWEEN 0 AND 1),
            PRIMARY KEY (chunk_id, evidence_kind, evidence_key, source)
        );

        -- The scoring-time lookup: given gold evidence, which chunks are it?
        CREATE INDEX IF NOT EXISTS chunk_evidence_lookup_idx
            ON chunk_evidence (repo_id, evidence_kind, evidence_key);

        -- The reverse direction, for explaining a retrieved chunk in the UI.
        CREATE INDEX IF NOT EXISTS chunk_evidence_by_chunk_idx
            ON chunk_evidence (chunk_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS chunk_evidence_by_chunk_idx;
        DROP INDEX IF EXISTS chunk_evidence_lookup_idx;
        DROP TABLE IF EXISTS chunk_evidence;
        """
    )
