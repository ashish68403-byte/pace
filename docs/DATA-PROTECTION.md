# Data protection statement — PACE

*Provenance-Aware Code Intelligence. Final-year undergraduate project. Single researcher,
single machine, no third-party data sharing.*
Last reviewed: 2026-09-10.

## 1. What personal data is processed

PACE indexes public development discussion from the `apache/airflow` repository on GitHub.
The following categories of personal data are unavoidably present and are retained:

| Data | Where it comes from | Why it is necessary |
|---|---|---|
| **Author login** (e.g. `potiuk`) | GitHub API: `user.login` on issues, PRs, comments; `author.login` on commits | It is the citation. The project's central claim is that every answer names the artefact and the person who wrote the rationale. Removing it would remove the thing being evaluated. Logins are self-chosen public pseudonyms already published by the data subject. |
| **Comment, issue, PR and commit-message text** | GitHub API | This *is* the research object: the corpus of written rationale the system retrieves over. Such text may incidentally contain personal data the author chose to publish about themselves or others. |
| **Timestamps** (`created_at`, authored/committed dates) | GitHub API / git | Needed to order provenance and to test whether the system prefers recent rationale over stale rationale. |

## 2. What is explicitly excluded

| Excluded | How |
|---|---|
| **Commit author and committer email addresses** | Hashed with SHA-256 at ingestion (`hash_email()` in `scripts/fetch_corpus.py`), truncated to 32 hex chars, and stored only as `author_email_sha256`. The raw address is never written to disk, the database, the manifest, or a log. |
| **Email addresses appearing in body text** | Regex-scrubbed to `<email:…>` **before** hashing and **before** caching (`scrub_emails()`). The stored sha256 describes the scrubbed bytes, so no unscrubbed copy is ever needed for verification. |
| **Real names, avatars, profile URLs, organisation membership, follower graphs** | Never requested. Only `login` is read from the `user` object. |
| **Private repository data, private profile data, anything requiring elevated scope** | Read-only public-scope token; no endpoint touched requires more. |
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
(repo, pr_number, issue_number, comment_id, commit_sha, sha256_of_body, url)
```

Bodies are fetched at run time from the GitHub API into a gitignored local cache
(`~/corpus/airflow/bodies`). The committed `sha256_of_body` makes the corpus
*reproducible* — anyone re-running `fetch_corpus.py hydrate` can prove they obtained the
same bytes — without the repository being a *copy*.

GitHub's Acceptable Use Policies permit collecting information via the API for research
purposes **on the condition that any resulting publications are open access**. This
project is submitted as an open-access dissertation, and the code and manifest are
published openly, which is what satisfies that condition. The API is accessed with an
authenticated read-only token, respecting documented rate limits (primary limits are
waited out, secondary limits backed off) — no scraping of the web UI.

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
| ID manifest (`corpus/manifest.jsonl`) | Life of the project + the examination period, then retained as part of the open-access publication because it contains no body text. |
| Hydrated body cache (local, gitignored) | Deleted at project completion, or on request. Rebuildable at any time from the manifest. |
| Database (`pace-db` volume) | Deleted at project completion (`docker volume rm pace-pgdata`). |
| Evaluation reports (`eval/reports/*.json`) | Retained — they contain metrics and query ids, no personal data. |
| Golden set | Questions are researcher-authored; gold evidence is anchors only (`commit_sha`, `pr_number`, `issue_number`, `review_comment_id`, `(path, qualified_name)`). No body text, no personal data beyond what an anchor implies. |

**Erasure requests.** Because bodies are never committed and are re-derived from GitHub
at run time, deletion upstream propagates automatically: a comment deleted on GitHub
cannot be re-hydrated and its cached copy is removed by the next
`fetch_corpus.py verify` run. A request from an individual to exclude their contributions
is honoured by filtering their login out of the manifest and re-running ingestion; the
contact address is on the dissertation cover page.

## 6. Security

Single-machine processing. The database is bound to `localhost` only and is not exposed
to the network. The GitHub token is read-only and supplied via environment variable, never
committed. No personal data leaves the machine: retrieval and the eval pipeline run
locally, and the only outbound calls are to the GitHub API (fetching data, not sending it)
and — in the generation half only, never in the deterministic retrieval gate — to an LLM
provider, where the payload is corpus text already public on GitHub.

## 7. Ethics review

No human participants, no interventions, no recruitment, no sensitive-category data
(Art. 9). Public data only, minimised as above. This statement is the artefact submitted
for institutional ethics self-assessment.
