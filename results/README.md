# Results

The numbers the write-up quotes. Each file here comes from one command, records the corpus commit
and the exact git commands behind it, and re-running that command on the same corpus and graph
reproduces it byte for byte.

| File | Command | What it measures |
|---|---|---|
| `history_truncation.json` | `uv run pace measure truncation` | For every in-scope file: its history by today's path, rename-aware, and by `git log --follow` |

`artifacts/` is different: gitignored scratch that CI writes and uploads.
