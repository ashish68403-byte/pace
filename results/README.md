# Results

The numbers the write-up quotes. Each file here comes from one command, records the corpus commit
and the exact git commands behind it, and re-running that command on the same corpus and graph
reproduces it byte for byte.

| File | Command | What it measures |
|---|---|---|
| `history_truncation.json` | `uv run pace measure truncation` | For every in-scope file: its history by today's path, rename-aware, and by `git log --follow` |
| `pool_census.json` | `uv run pace measure pool` | The benchmark's commit-message pool: documents, PR links and why some are dropped, message lengths |
| `golden_census.json` | `uv run pace golden generate` | The golden set's census: candidates per rule, leakage-filter outcomes per template, the split, dropped examples. Its records are `provenance/eval/golden/auto_full.jsonl` |
| `leakage_verdict.json` | `uv run pace leakage --from-db --no-dense --golden provenance/eval/golden/auto_full.jsonl --json-out results/leakage_verdict.json` | Checkpoint B's leakage verdict on the dev split (ADR-0003 §6): every lexical baseline with its Wilson interval, before and after the filters |

`artifacts/` is different: gitignored scratch that CI writes and uploads.
