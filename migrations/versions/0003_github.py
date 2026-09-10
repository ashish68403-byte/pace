"""0003_github: PRs, issues, comments, and the TYPED provenance edge tables.

===========================================================================
ADR-0001  Typed edge tables, not one polymorphic `edges` table
---------------------------------------------------------------------------
Status: accepted, week 1. Supersedes the obvious design.

Context
    The graph has to link commits to PRs, PRs to issues, and symbols to
    commits. The tempting shape is one table:

        edges(src_kind, src_id text, dst_kind, dst_id text, relation, source)

    It is one migration, one insert path, one traversal query, and on
    apache/airflow it would hold roughly 5M rows.

Decision
    Three typed tables -- edge_commit_pr, edge_pr_issue, edge_symbol_commit --
    plus a `provenance_edges` VIEW that unions them for generic traversal.

Why (1): only typed tables get real foreign keys.
    In the polymorphic table src_id must be text to hold a 40-char sha, a PR
    number and a bigint lineage_id at once. text cannot reference
    commits(repo_id, commit_sha) or pull_requests(repo_id, pr_number). The
    referential integrity of the entire provenance graph then lives in Python,
    and the first driver that writes a link to a PR it never ingested creates
    a dangling citation. A citation that points at nothing is precisely the
    failure this project exists to avoid: it looks like evidence and is not.
    The typed tables carry composite FKs, so that row cannot be written.

Why (2): one histogram is the wrong statistics for three populations.
    A single 5M-row table gives the planner ONE set of column statistics.
    n_distinct on `src_id` is computed across shas, PR numbers and lineage ids
    mixed together, and selectivity for `relation = 'closes'` is one global
    fraction. The hot query is the 3-hop path -- chunk -> commit -> PR -> issue
    -- and the planner picks join order from those estimates. With the mixed
    histogram it consistently misjudges the middle hop, chooses a hash join
    over the whole edge table instead of a nested loop over ~10 rows, and the
    3-hop traversal goes from milliseconds to seconds. Three tables give three
    honest histograms and a correct join order for free.

Costs accepted
    Adding an edge type means a migration, and the union view must be kept in
    step. Both are cheap; a wrong join order on the hot path is not.
===========================================================================

What this revision is for: the written rationale. Code says what; PRs, issues
and inline review comments say why. If the why was never written down, the
system must be able to prove that and refuse -- which requires knowing what we
successfully ingested and what merely failed.

What failure it prevents:
  * Review comments that drift. A review comment is anchored to a diff, and a
    force-push rewrites the diff. Anchoring on `line` / `commit_id` silently
    re-points a 2019 comment at whatever code now occupies that line number.
  * Unfalsifiable link claims. Every edge carries a `source` under a CHECK
    constraint, so `SELECT source, count(*) FROM edge_pr_issue GROUP BY 1`
    answers "how much of this graph is structural and how much is regex?"
    live, in a viva, in one query.
"""

from __future__ import annotations

from alembic import op

revision: str = "0003_github"
down_revision: str | None = "0002_lineage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # pull_requests
    # `raw` keeps the untouched API payload: the schema will be wrong about
    # some field in week 6, and re-fetching 40k PRs against a rate limit is
    # not an option.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pull_requests (
            repo_id          bigint      NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            pr_number        integer     NOT NULL,
            node_id          text,
            title            text        NOT NULL DEFAULT '',
            body             text        NOT NULL DEFAULT '',
            state            text        NOT NULL,
            is_draft         boolean     NOT NULL DEFAULT false,
            author_login     text,
            author_association text,
            created_at       timestamptz NOT NULL,
            updated_at       timestamptz,
            closed_at        timestamptz,
            merged_at        timestamptz,
            merge_commit_sha git_sha,
            head_sha         git_sha,
            base_ref         text,
            head_ref         text,
            additions        integer     NOT NULL DEFAULT 0,
            deletions        integer     NOT NULL DEFAULT 0,
            changed_files    integer     NOT NULL DEFAULT 0,
            review_comment_count integer NOT NULL DEFAULT 0,
            labels           text[]      NOT NULL DEFAULT '{}',
            raw              jsonb       NOT NULL DEFAULT '{}'::jsonb,
            fetched_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (repo_id, pr_number),
            CONSTRAINT pull_requests_state_known CHECK (state IN ('open', 'closed', 'merged')),
            CONSTRAINT pull_requests_number_positive CHECK (pr_number > 0)
        );

        CREATE INDEX IF NOT EXISTS pull_requests_merged_idx
            ON pull_requests (repo_id, merged_at DESC)
            WHERE merged_at IS NOT NULL;

        CREATE INDEX IF NOT EXISTS pull_requests_body_trgm_idx
            ON pull_requests USING gin (body gin_trgm_ops);
        """
    )

    # ------------------------------------------------------------------
    # issues
    # GitHub returns PRs from the issues endpoint too; is_pull_request keeps
    # them distinguishable instead of double-counting the corpus.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS issues (
            repo_id         bigint      NOT NULL REFERENCES repositories(repo_id) ON DELETE CASCADE,
            issue_number    integer     NOT NULL,
            node_id         text,
            title           text        NOT NULL DEFAULT '',
            body            text        NOT NULL DEFAULT '',
            state           text        NOT NULL,
            state_reason    text,
            author_login    text,
            labels          text[]      NOT NULL DEFAULT '{}',
            comment_count   integer     NOT NULL DEFAULT 0,
            created_at      timestamptz NOT NULL,
            updated_at      timestamptz,
            closed_at       timestamptz,
            is_pull_request boolean     NOT NULL DEFAULT false,
            raw             jsonb       NOT NULL DEFAULT '{}'::jsonb,
            fetched_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (repo_id, issue_number),
            CONSTRAINT issues_state_known CHECK (state IN ('open', 'closed')),
            CONSTRAINT issues_number_positive CHECK (issue_number > 0)
        );

        CREATE INDEX IF NOT EXISTS issues_labels_idx ON issues USING gin (labels);
        CREATE INDEX IF NOT EXISTS issues_body_trgm_idx
            ON issues USING gin (body gin_trgm_ops);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_comments (
            comment_id   bigint      NOT NULL PRIMARY KEY,
            repo_id      bigint      NOT NULL,
            issue_number integer     NOT NULL,
            node_id      text,
            author_login text,
            author_association text,
            body         text        NOT NULL DEFAULT '',
            created_at   timestamptz NOT NULL,
            updated_at   timestamptz,
            raw          jsonb       NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT issue_comments_issue_fk
                FOREIGN KEY (repo_id, issue_number)
                REFERENCES issues(repo_id, issue_number) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS issue_comments_issue_idx
            ON issue_comments (repo_id, issue_number, created_at);
        """
    )

    # ------------------------------------------------------------------
    # review_comments -- the highest-value rationale in the corpus and the
    # easiest to anchor wrongly.
    #
    # Anchor on original_line + original_commit_sha, NOT on line/commit_sha.
    # A review comment is attached to a diff hunk. When the author force-pushes
    # or rebases, GitHub recomputes `line` and `commit_id` against the new
    # diff, or gives up and marks the comment outdated. The original_* pair is
    # the only stable coordinate: it names the exact blob state the reviewer
    # was actually looking at when they wrote the sentence we intend to quote.
    #
    # The column is diff_position, not `position`: POSITION is a reserved
    # keyword in Postgres (SQL string function), so an unquoted
    # `SELECT position FROM review_comments` is a syntax error and every
    # hand-written query would need double quotes forever.
    #
    # is_resolved and is_outdated are GraphQL-only. The REST pulls endpoint
    # does not expose resolution state at all -- it lives on
    # PullRequestReviewThread. They are nullable because the REST backfill
    # legitimately cannot fill them, and "unknown" must not be stored as
    # "false": an unresolved-looking thread that was actually resolved is a
    # citation of an objection that was already answered.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS review_comments (
            review_comment_id   bigint      NOT NULL PRIMARY KEY,
            repo_id             bigint      NOT NULL,
            pr_number           integer     NOT NULL,
            node_id             text,
            review_id           bigint,
            thread_node_id      text,
            in_reply_to_id      bigint,
            author_login        text,
            author_association  text,
            body                text        NOT NULL DEFAULT '',
            path                text        NOT NULL,
            diff_hunk           text,
            diff_position       integer,
            original_position   integer,
            original_line       integer,
            original_start_line integer,
            original_commit_sha git_sha,
            commit_sha          git_sha,
            line                integer,
            start_line          integer,
            side                text,
            start_side          text,
            subject_type        text,
            is_resolved         boolean,
            is_outdated         boolean,
            created_at          timestamptz NOT NULL,
            updated_at          timestamptz,
            raw                 jsonb       NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT review_comments_pr_fk
                FOREIGN KEY (repo_id, pr_number)
                REFERENCES pull_requests(repo_id, pr_number) ON DELETE CASCADE,
            CONSTRAINT review_comments_side_known CHECK (
                side IS NULL OR side IN ('LEFT', 'RIGHT')
            )
        );

        -- The anchor the retriever resolves against a file revision.
        CREATE INDEX IF NOT EXISTS review_comments_anchor_idx
            ON review_comments (repo_id, path, original_commit_sha, original_line);

        CREATE INDEX IF NOT EXISTS review_comments_pr_idx
            ON review_comments (repo_id, pr_number, created_at);

        CREATE INDEX IF NOT EXISTS review_comments_thread_idx
            ON review_comments (in_reply_to_id)
            WHERE in_reply_to_id IS NOT NULL;

        -- Rows the REST backfill could not populate; the GraphQL pass targets
        -- exactly these instead of re-walking every comment.
        CREATE INDEX IF NOT EXISTS review_comments_needs_graphql_idx
            ON review_comments (repo_id, pr_number)
            WHERE is_resolved IS NULL;
        """
    )

    # ------------------------------------------------------------------
    # Typed edge tables. See ADR-0001 at the top of this file.
    #
    # `source` is part of the primary key, not a mere annotation: the same
    # link found by a structural event AND by a regex is two rows, and
    #     SELECT source, count(*) FROM edge_pr_issue GROUP BY 1 ORDER BY 2 DESC;
    # then reports the real provenance mix of the graph. The CHECK constraint
    # keeps that vocabulary closed -- a typo'd source would quietly become its
    # own category and make the number meaningless.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS edge_commit_pr (
            repo_id    bigint  NOT NULL,
            commit_sha git_sha NOT NULL,
            pr_number  integer NOT NULL,
            source     text    NOT NULL,
            confidence real    NOT NULL DEFAULT 1.0,
            evidence   jsonb   NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (repo_id, commit_sha, pr_number, source),
            CONSTRAINT edge_commit_pr_commit_fk
                FOREIGN KEY (repo_id, commit_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT edge_commit_pr_pr_fk
                FOREIGN KEY (repo_id, pr_number)
                REFERENCES pull_requests(repo_id, pr_number) ON DELETE CASCADE,
            CONSTRAINT edge_commit_pr_source_known CHECK (source IN (
                'merge_commit_sha',   -- structural: pull_requests.merge_commit_sha
                'pr_commits_api',     -- structural: /pulls/{n}/commits
                'squash_message_ref', -- regex over '(#1234)' in the squashed subject
                'branch_ref',         -- head_ref matched a local branch tip
                'manual'              -- hand-curated, used by the golden set
            )),
            CONSTRAINT edge_commit_pr_confidence_range
                CHECK (confidence >= 0.0 AND confidence <= 1.0)
        );

        CREATE INDEX IF NOT EXISTS edge_commit_pr_pr_idx
            ON edge_commit_pr (repo_id, pr_number);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS edge_pr_issue (
            repo_id      bigint        NOT NULL,
            pr_number    integer       NOT NULL,
            issue_number integer       NOT NULL,
            relation     edge_relation NOT NULL,
            source       text          NOT NULL,
            confidence   real          NOT NULL DEFAULT 1.0,
            evidence     jsonb         NOT NULL DEFAULT '{}'::jsonb,
            created_at   timestamptz   NOT NULL DEFAULT now(),
            PRIMARY KEY (repo_id, pr_number, issue_number, relation, source),
            CONSTRAINT edge_pr_issue_pr_fk
                FOREIGN KEY (repo_id, pr_number)
                REFERENCES pull_requests(repo_id, pr_number) ON DELETE CASCADE,
            CONSTRAINT edge_pr_issue_issue_fk
                FOREIGN KEY (repo_id, issue_number)
                REFERENCES issues(repo_id, issue_number) ON DELETE CASCADE,
            CONSTRAINT edge_pr_issue_source_known CHECK (source IN (
                'timeline_event',   -- structural: cross-referenced/closed event
                'closing_keyword',  -- GitHub-parsed 'Fixes #N' (closingIssuesReferences)
                'body_reference',   -- '#N' in the PR body, no closing keyword
                'comment_regex',    -- '#N' in a comment
                'manual'
            )),
            CONSTRAINT edge_pr_issue_confidence_range
                CHECK (confidence >= 0.0 AND confidence <= 1.0)
        );

        CREATE INDEX IF NOT EXISTS edge_pr_issue_issue_idx
            ON edge_pr_issue (repo_id, issue_number);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS edge_symbol_commit (
            repo_id    bigint        NOT NULL,
            lineage_id bigint        NOT NULL REFERENCES symbol_lineage(lineage_id) ON DELETE CASCADE,
            commit_sha git_sha       NOT NULL,
            relation   edge_relation NOT NULL,
            source     text          NOT NULL,
            confidence real          NOT NULL DEFAULT 1.0,
            evidence   jsonb         NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz   NOT NULL DEFAULT now(),
            PRIMARY KEY (repo_id, lineage_id, commit_sha, relation, source),
            CONSTRAINT edge_symbol_commit_commit_fk
                FOREIGN KEY (repo_id, commit_sha)
                REFERENCES commits(repo_id, commit_sha) ON DELETE CASCADE,
            CONSTRAINT edge_symbol_commit_source_known CHECK (source IN (
                'ast_diff',          -- structural: the symbol's body changed
                'blame',             -- a blame range inside the symbol points here
                'rename_detect',     -- git rename detection carried it across
                'lineage_edge',      -- inferred via an extract/inline/split/merge edge
                'message_regex',     -- the commit message names the symbol
                'manual'
            )),
            CONSTRAINT edge_symbol_commit_confidence_range
                CHECK (confidence >= 0.0 AND confidence <= 1.0)
        );

        CREATE INDEX IF NOT EXISTS edge_symbol_commit_commit_idx
            ON edge_symbol_commit (repo_id, commit_sha);
        """
    )

    # ------------------------------------------------------------------
    # provenance_edges: one generic surface for the agent's traversal, over
    # three typed tables underneath. Ids are widened to text HERE, at read
    # time, where nothing can be inserted through them. UNION ALL, not UNION:
    # the branches are disjoint by construction and dedup would cost a sort
    # over the whole graph.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE VIEW provenance_edges AS
            SELECT repo_id,
                   'pull_request'::artifact_kind AS src_kind,
                   pr_number::text               AS src_id,
                   'commit'::artifact_kind       AS dst_kind,
                   commit_sha::text              AS dst_id,
                   'contains'::edge_relation     AS relation,
                   source, confidence, evidence, created_at
            FROM edge_commit_pr
        UNION ALL
            SELECT repo_id,
                   'pull_request'::artifact_kind AS src_kind,
                   pr_number::text               AS src_id,
                   'issue'::artifact_kind        AS dst_kind,
                   issue_number::text            AS dst_id,
                   relation,
                   source, confidence, evidence, created_at
            FROM edge_pr_issue
        UNION ALL
            SELECT repo_id,
                   'symbol'::artifact_kind       AS src_kind,
                   lineage_id::text              AS src_id,
                   'commit'::artifact_kind       AS dst_kind,
                   commit_sha::text              AS dst_id,
                   relation,
                   source, confidence, evidence, created_at
            FROM edge_symbol_commit;

        COMMENT ON VIEW provenance_edges IS
            'Read-only union of the typed edge tables. Write to the typed '
            'tables; only they carry composite FKs. See ADR-0001 in '
            'migrations/versions/0003_github.py.';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW  IF EXISTS provenance_edges;
        DROP TABLE IF EXISTS edge_symbol_commit CASCADE;
        DROP TABLE IF EXISTS edge_pr_issue      CASCADE;
        DROP TABLE IF EXISTS edge_commit_pr     CASCADE;
        DROP TABLE IF EXISTS review_comments    CASCADE;
        DROP TABLE IF EXISTS issue_comments     CASCADE;
        DROP TABLE IF EXISTS issues             CASCADE;
        DROP TABLE IF EXISTS pull_requests      CASCADE;
        """
    )
