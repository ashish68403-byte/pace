# Task 004: Keep raw email addresses out of the database

**Tracker:** [001_INGEST](../../tracker/001_INGEST.md) · task 7 · fixes checkpoint A, before checkpoint B slice B2
**Priority:** P0 · **Status:** ✅ Done (2026-10-27)
**Governing statement:** [DATA-PROTECTION.md](../../DATA-PROTECTION.md) §1–§2

## User Story

- **Summary:** Checkpoint A's git ingest stored the raw email addresses of commit authors and
  committers, and message text containing addresses, in the database. DATA-PROTECTION.md §2 says
  that never happens. Make the ingest honour the statement, rewrite the rows already stored, make
  the database itself refuse a raw address from now on, and record the incident.

### Use Case

- **As the** author whose ethics self-assessment is DATA-PROTECTION.md
- **I want** the code to do what the statement says, and to be able to show it
- **so that** "the raw address is never written to the database" is a property the schema
  enforces, not only a sentence.

### Background (measured 2026-10-27 on `pace`)

- `commits.author_email` and `commits.committer_email` hold a raw address on **all 38,888** rows.
- **4,767** messages contain an email-like token. 760 of them are among the 6,810 commits that
  touch an in-scope file, the pool slice B2 builds. 767 pool messages carry `Co-authored-by` or
  `Signed-off-by` trailers.
- Nowhere else: 0 dead letters and 0 `ingest_runs` records contain one, and neither do the run
  logs in `~/pace-plans/runs/` or any committed file under `results/`, `eval/` or
  `provenance/eval/golden/`. The database is bound to localhost, so no address left the machine.
- **Cause:** `hash_email()` and `scrub_emails()` exist only in `scripts/fetch_corpus.py`, the
  GitHub path. The git path (`provenance/ingest/git_walk.py`, written for checkpoint A) never calls
  them.
- Nothing reads the email columns, nor the name columns.
- A second route exists: a rejected commit's dead letter stores its subject unscrubbed
  (`git_walk.py:1148`).

## Design

### One implementation

`provenance/privacy.py` (new) holds `EMAIL_RE`, `hash_email()` and `scrub_emails()`, moved
verbatim from `scripts/fetch_corpus.py`, which then imports them. The git path and the GitHub path
(slice B4) then produce the same hash for the same address, so "same author" stays a join key
across both sources.

### The parser is the boundary

`_parse_raw_record` is the only place a raw address exists, and only as a local variable. It:

- computes `bot_author = is_bot_author(author_name, author_email)` from the raw values.
  `BOT_AUTHOR_RE` matches `noreply@github.com$` against the email, so this must happen before
  hashing;
- stores `author_email_sha256 = hash_email(email) if email else None`, and the same for the
  committer;
- stores `subject = scrub_emails(subject)` and `message = scrub_emails(message)`.

`CommitRecord` loses `author_email` and `committer_email`, and gains `author_email_sha256`,
`committer_email_sha256` and `bot_author`. `is_mechanical()` takes `bot_author: bool` in place of
the author's name and email, so every verdict is unchanged. Every consumer after the parser (the
classifier, the writer, the dead-letter subject) sees only redacted values. The writer
(`graph_writer.py`) changes column names only.

### Migration `0008_hashed_emails`

In one transaction:

1. **Rename** `commits.author_email` to `author_email_sha256`, and `committer_email` to
   `committer_email_sha256`.
2. **Rewrite** every email value that is not already 32 lowercase hex characters (an empty value
   becomes NULL, as the parser now writes; none exist today), and scrub every subject and message
   that matches the email pattern. This is the project's first migration
   with a Python data step. It uses a frozen copy of the two helpers, because a migration must
   behave the same after the package changes; a unit test pins the copy to `provenance/privacy.py`.
   Scrubbing is idempotent, so a re-run changes nothing.
3. **Constrain:** `commits_author_email_hashed` and `commits_committer_email_hashed` require NULL
   or `^[0-9a-f]{32}$`; `commits_no_raw_email` requires that neither subject nor message matches
   the pattern. From then on the database refuses a raw address, even one written by a future bug.

The downgrade drops the CHECKs and renames the columns back. The values stay hashed and scrubbed:
no downgrade restores a raw address, by design.

### Names are kept, and declared

Git author and committer names stay as stored. DATA-PROTECTION.md §1 gains a row for them: they
come from the public, Apache-licensed history and are the citation for commit-sourced rationale, as
logins are for GitHub text.

### Documentation

DATA-PROTECTION.md gets four updates:
- §1: the names row;
- §2: rows that point at `provenance/privacy.py` and name both ingestion paths;
- a dated incident note: what was stored, where, for how long, what was checked, and how it was
  fixed;
- a new "Last reviewed" date.

## Acceptance Criteria

- **Scenario:** one implementation
  - **Given:** `provenance/privacy.py`
  - **When:** the GitHub path and the git path redact
  - **Then:** both import `hash_email` and `scrub_emails` from it, and `scripts/fetch_corpus.py`
    defines neither.

- **Scenario:** the parser emits no raw address
  - **Given:** a log record with author `ada@example.com`, committer `grace@example.com`, and a
    message carrying `Co-authored-by: Lin <lin@example.org>`
  - **When:** `_parse_raw_record` parses it
  - **Then:** the record carries `hash_email("ada@example.com")` and
    `hash_email("grace@example.com")`, the message carries an `<email:…>` marker in place of the
    address, and no string field of the record matches `EMAIL_RE`.

- **Scenario:** bot verdicts are unchanged
  - **Given:** authors `dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>`,
    `GitHub <noreply@github.com>` and `Ada <ada@example.com>`
  - **When:** each is parsed and classified
  - **Then:** the first two are mechanical with reason `bot-author` and the third is not, the same
    verdicts as before the change.

- **Scenario:** scrubbing is idempotent
  - **Given:** text containing addresses
  - **Then:** `scrub_emails(scrub_emails(t)) == scrub_emails(t)`.

- **Scenario:** the migration's frozen helpers match the package
  - **Given:** sample addresses and texts
  - **Then:** the copies inside `0008_hashed_emails` return exactly what `provenance/privacy.py`
    returns.

- **Scenario:** a fresh ingest stores no address
  - **Given:** the hermetic fixture repository (author and committer `fixture@example.com`), with
    one commit whose message carries an address
  - **When:** it is ingested
  - **Then:** every `author_email_sha256` equals `hash_email("fixture@example.com")`, and no subject
    or message matches the pattern.

- **Scenario:** the database refuses a raw address
  - **Given:** migration 0008
  - **When:** a row with a raw address in `author_email_sha256`, or with an address in `message`,
    is inserted
  - **Then:** the insert fails on the named CHECK.

- **Scenario:** stored rows are rewritten exactly
  - **Given:** a throwaway database at 0007 holding rows with raw addresses
  - **When:** it is upgraded to 0008
  - **Then:** each row holds exactly what the fixed parser writes for the same values, and the
    CI-style double round-trip still passes.

- **Scenario:** only personal data changed, on real data
  - **Given:** `pace` at 0007, holding checkpoint A's graph
  - **When:** `alembic upgrade head` runs
  - **Then:**
    - rows with a raw address in either email column go from 38,888 to 0;
    - messages containing one go from 4,767 to 0;
    - the mechanical count stays at 2,223 (624 of them `bot-author`);
    - `pace measure truncation` regenerates `results/history_truncation.json` byte-identical
      (sha256 `b49198dc…`, no `git diff`).

- **Scenario:** the statement matches the code
  - **Given:** DATA-PROTECTION.md
  - **Then:** §1 lists git names, §2 names `provenance/privacy.py` and both ingestion paths, and the
    incident note is dated and states the measured extent.

## Files to Modify

- `provenance/privacy.py`: new
- `scripts/fetch_corpus.py`: imports the helpers instead of defining them
- `provenance/ingest/git_walk.py`: parser boundary, `CommitRecord` fields, `is_bot_author()`,
  `is_mechanical()` signature
- `provenance/ingest/graph_writer.py`: column names
- `migrations/versions/0008_hashed_emails.py`: new
- `tests/unit/test_privacy.py` (new), `tests/unit/test_git_walk_parse.py`,
  `tests/integration/test_schema.py` (head 0008, the CHECKs), `tests/integration/test_graph_writer.py`
  (a fresh ingest stores no address)
- `docs/DATA-PROTECTION.md`, `docs/tracker/001_INGEST.md`

## Verification

    export PACE_DATABASE_URL="postgresql://postgres:pace@localhost:5432/pace"
    uv run pytest tests -q
    uv run ruff check . && uv run ruff format --check . && uv run mypy provenance
    # In a throwaway database (never downgrade `pace`): upgrade to 0007, insert rows holding raw
    # addresses, upgrade to head and compare them with the parser's output, then the double
    # round-trip. The plan gives the exact commands.
    docker exec pace-db createdb -U postgres pace_roundtrip
    docker exec pace-db dropdb -U postgres pace_roundtrip
    # real data
    uv run alembic upgrade head
    docker exec pace-db psql -U postgres -d pace -c "SELECT count(*) FROM commits
      WHERE author_email_sha256 !~ '^[0-9a-f]{32}$' OR committer_email_sha256 !~ '^[0-9a-f]{32}$';"
    uv run pace measure truncation && git diff --exit-code results/history_truncation.json
