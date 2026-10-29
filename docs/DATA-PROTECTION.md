# Data protection statement — PACE

*Provenance-Aware Code Intelligence. Final-year undergraduate project. Single researcher,
single machine, no third-party data sharing.*
Last reviewed: 2026-10-29.

## 1. What personal data is processed

PACE indexes public development discussion from the `apache/airflow` repository on GitHub.
The following categories of personal data are unavoidably present and are retained:

| Data | Where it comes from | Why it is necessary |
|---|---|---|
| **Author login** (e.g. `potiuk`) | GitHub API: `user.login` on the PRs fetched and on their comments | It is the citation. The project's central claim is that every answer names the artefact and the person who wrote the rationale. Removing it would remove the thing being evaluated. Logins are self-chosen public pseudonyms already published by the data subject. |
| **Git author and committer names** | git history: the author and committer recorded in each commit | How the commit itself names the person, and so the citation for commit-sourced rationale, as the login is for GitHub text. They are part of the public, Apache-licensed history the project analyses, and are stored as recorded. |
| **PR, comment and commit-message text** | GitHub API (PRs and their comments); git history (commit messages) | This *is* the research object: the corpus of written rationale the system retrieves over. Such text may incidentally contain personal data the author chose to publish about themselves or others. |
| **Timestamps** (`created_at`, authored/committed dates) | GitHub API / git | Needed to order provenance and to test whether the system prefers recent rationale over stale rationale. |

**Scope of the GitHub fetch** (checkpoint B, slice B4): the 5,963 apache/airflow PRs that
in-scope commits reference — each PR's title and description, its inline review comments and its
conversation comments. No issues and no other PRs are fetched.

## 2. What is explicitly excluded

| Excluded | How |
|---|---|
| **Commit author and committer email addresses** | Hashed with SHA-256 at ingestion (`hash_email()` in `provenance/privacy.py`), truncated to 32 hex chars, by the git walk (`provenance/ingest/git_walk.py`, stored as `commits.author_email_sha256` and `commits.committer_email_sha256`). The GitHub fetch (`provenance/ingest/github_backfill.py`) requests no commit data, so it never receives an author address. The raw address is never written to disk, the database, the manifest, or a log, and the database refuses one: migration `0008_hashed_emails` adds CHECK constraints. §8 records the one time this did not hold. |
| **Email addresses appearing in body text** | Regex-scrubbed to `<email:…>` (`scrub_emails()` in `provenance/privacy.py`): **before** storage and **before** hashing on the GitHub path, in every text field it keeps (titles, descriptions, comments, diff hunks), and before storage on the git path (commit subjects and messages). The manifest's SHA-256 describes the scrubbed text, so no unscrubbed copy is ever needed for verification. |
| **Real names, avatars, profile URLs, organisation membership, follower graphs** | Never stored. GitHub's responses carry avatars and profile URLs; from the `user` object only `login` and whether the account type is `Bot` are kept. The raw response is discarded, and neither `author_association` (it reveals organisation membership) nor a PR's `head_ref` (a fork's branch name can carry a person's name) is stored. |
| **Private repository data, private profile data, anything requiring elevated scope** | A fine-grained token with read-only access to public repositories; no endpoint touched requires more. |
| **End-user query data** | There are no end users. Queries are the researcher's own and the golden set's. |

There is no attempt to re-identify hashed emails, and no rainbow-table defence is
claimed for the hash — it is a pseudonymisation measure to keep addresses out of the
artefact, not a security control. The correct mitigation is that the address is never
stored at all.

## 3. Copyright and redistribution — why only IDs are committed

Airflow's **source code** is Apache-2.0. The **discussion around it is not**: each issue
body, PR description and review comment remains the copyright of its author, licensed to
GitHub under GitHub's Terms of Service, not to third parties under Apache-2.0.
Committing that text into this repository would redistribute several thousand people's
writing without a licence to do so.

Therefore the repository commits **identifiers only**:

```
(kind, pr_number, comment_id, sha256 of the stored text)
```

Text is fetched from the GitHub API into the local database (`pace-db`, bound to localhost),
never into the repository. The committed manifest, `results/github_manifest.jsonl`, holds one
line per stored artifact, with no text and no login. It makes the corpus *reproducible* —
anyone re-running `pace github backfill` can prove they obtained the same bytes, or count how
many were edited since — without the repository being a *copy*.

GitHub's Acceptable Use Policies permit collecting information via the API for research
purposes **on the condition that any resulting publications are open access**. This
project is submitted as an open-access dissertation, and the code and manifest are
published openly, which is what satisfies that condition. The API is accessed with an
authenticated read-only token, respecting documented rate limits (primary limits are
waited out, secondary limits backed off), under a request budget of one request per PR plus
one per page of each kind of its comments — no scraping of the web UI.

## 4. Lawful basis

- **UK GDPR Art. 6(1)(f) — legitimate interests**, the interest being academic research
  into software provenance and retrieval evaluation.
  - *Necessity:* the research question is "can a system retrieve the recorded rationale
    for a code decision and refuse when none exists?" It cannot be answered without the
    rationale text and its authorship.
  - *Balancing:* the data is already public, was published by the data subjects
    themselves in a public forum for the express purpose of being read and referred to,
    and is used here only to cite them as the source of their own words — the same use a
    human reader of the PR makes. The most sensitive field available (email) is excluded
    entirely. Volume is limited to one repository.
- **Art. 89 safeguards for research** apply: pseudonymisation where possible (emails
  hashed), data minimisation (only the fields listed in §1), no decisions are made about
  individuals, and no attempt is made to profile, rank or evaluate contributors. PACE
  evaluates *itself*, not the people whose comments it indexes.
- Processing is **not** for direct marketing, automated decision-making, or any purpose
  producing legal or similarly significant effects on a data subject.

## 5. Retention and deletion

| Item | Retention |
|---|---|
| ID manifest (`results/github_manifest.jsonl`) | Retained as part of the open-access publication: it contains no text and no login. |
| Fetched text (the `pace-db` tables `pull_requests`, `review_comments` and `issue_comments`, and the pool documents in `chunks`) | Deleted with the database at project completion, or on request. Re-fetchable at any time with `pace github backfill`. |
| Database (`pace-db` volume) | Deleted at project completion (`docker volume rm pace-pgdata`). |
| Evaluation reports (`eval/reports/*.json`) | Retained — they contain metrics and query ids, no personal data. |
| Golden set | Questions are researcher-authored; gold evidence is anchors only (`commit_sha`, `pr_number`, `issue_number`, `review_comment_id`, `(path, qualified_name)`). No body text, no personal data beyond what an anchor implies. |

**Erasure requests.** Text is never committed, so nothing needs withdrawing from the
repository. A request from an individual to exclude their contributions is honoured by
deleting the rows that carry their login from the local database and rebuilding the pool
(`pace pool build`); the contact address is on the dissertation cover page. Text later deleted
on GitHub remains only in the local database, which is itself deleted at project completion.

## 6. Security

Single-machine processing. The database is bound to `localhost` only and is not exposed
to the network. The GitHub token is a fine-grained token with read-only access to public
repositories and a 30-day expiry. It is kept in the gitignored `.env` as `PACE_GITHUB_TOKEN`,
read as a secret value, and never logged, printed or stored. No personal data leaves the
machine: retrieval and the eval pipeline run locally, and the only outbound calls are to the
GitHub API (fetching data, not sending it) and — in the generation half only, never in the
deterministic retrieval gate — to an LLM provider, where the payload is corpus text already
public on GitHub.

## 7. Ethics self-assessment

**Self-assessment completed:** 2026-10-29, by the researcher, before the first fetch of GitHub
discussion text (checkpoint B, slice B4). `pace github backfill` refuses to run until this line
carries a date.

| Question | Answer |
|---|---|
| Human participants, interventions or recruitment? | None. |
| Source | Public text on github.com/apache/airflow, published by its authors to be read and cited. |
| Volume | The 5,963 PRs that in-scope commits reference: descriptions, inline review comments, conversation comments. No issues, no other repository. |
| Special-category data (Art. 9)? | None is sought; any present is incidental to public development discussion. |
| Minimisation | Logins and account type only; email addresses scrubbed from all text; avatars, profile URLs, organisation membership and raw API responses discarded (§2). |
| Profiling, or decisions about individuals? | None. PACE evaluates its own retrieval, not the people whose comments it indexes. |
| Storage | A local database bound to localhost, never committed, deleted at project completion (§5, §6). |
| Redistribution | None: the repository commits identifiers and hashes only (§3). |
| GitHub's terms | API access only, with a read-only token and its rate limits respected. The dissertation is open access, the condition GitHub's Acceptable Use Policies set for research use (§3). |

This statement is the artefact submitted for institutional ethics self-assessment.

## 8. Incidents

**2026-10-27 — email addresses stored by the git ingest.** Checkpoint A's git walk
(`provenance/ingest/git_walk.py`; first full run 2026-10-27, `ingest_runs` 87) stored commit
author and committer email addresses unhashed on all 38,888 commits, and stored subjects and
messages unscrubbed (4,767 contained an address): `hash_email()` and `scrub_emails()` existed
only on the GitHub path. The addresses were held only in the local `pace-db` database, bound to
localhost; no dead letter, ingest-run record, run log or committed file contained one (checked
2026-10-27). Fixed 2026-10-27: the parser now redacts before anything else sees a record, and
migration `0008_hashed_emails` rewrote the stored rows in place and added CHECK constraints, so
the database refuses a raw address. Verified on the real database: 0 raw addresses afterwards,
and checkpoint A's measurement reproduced byte-identical. Details:
[tasks/ingest/004](tasks/ingest/004_personal_data.md).
