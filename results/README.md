# Results

The numbers the write-up quotes. Each file here comes from one command, records the corpus commit
and the exact git commands behind it, and re-running that command on the same corpus and graph
reproduces it byte for byte.

| File | Command | What it measures |
|---|---|---|
| `history_truncation.json` | `uv run pace measure truncation` | For every in-scope file: its history by today's path, rename-aware, and by `git log --follow` |
| `pool_census.json` | `uv run pace measure pool` | The benchmark's pool: commit-message documents and PR links (B2), then the GitHub fetch — PR documents per kind, the request cost, and B2's links checked against GitHub's merge commits (B4) |
| `golden_census.json` | `uv run pace golden generate` | The golden set's census: candidates per rule, leakage-filter outcomes per template, the split, dropped examples. Its records are `provenance/eval/golden/auto_full.jsonl` |
| `leakage_verdict.json` | `uv run pace leakage --from-db --no-dense --golden provenance/eval/golden/auto_full.jsonl --json-out results/leakage_verdict.json` | Checkpoint B's leakage verdict on the dev split (ADR-0003 §6): every lexical baseline with its Wilson interval, before and after the filters |
| `github_manifest.jsonl` | `uv run pace github backfill` | One line per fetched PR description and comment: kind, PR number, comment id and the SHA-256 of the stored text. No text, no logins (DATA-PROTECTION.md §3) |
| `gold_validity_audit.json` | `uv run pace audit report` | ADR-0004's gold-validity audit: the dev set's validity rate from 56 stratified, labelled records with its interval; what PR text adds over the commit message; self-agreement once the 15-record re-label is in; reachability at k = 100; linked against unlinked commits. Its inputs are `provenance/eval/audit/` |

`artifacts/` is different: gitignored scratch that CI writes and uploads.
