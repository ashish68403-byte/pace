# PACE - developer entry points.
#
# CONTAINERS ARE DRIVEN BY docker-compose.yml. The compose plugin IS installed here
# (Docker Compose v2.40.3); the note that used to sit at the top of this file --
# "Don't add a compose file; it won't run" -- predates that and contradicted the
# compose file sitting next to it. Two files describing the same containers is how
# they came to disagree about the Jaeger image: this Makefile ran
# jaegertracing/all-in-one:1.62.0, which is the END-OF-LIFE v1 line, while
# docker-compose.yml correctly ran jaegertracing/jaeger:2.x and explained why.
#
# The fix is not to reconcile the two copies, it is to have one copy. Every container
# target below shells out to compose, so the image digests, the PG18 volume mount and
# the healthchecks are defined exactly once, in docker-compose.yml.
#
# The database digest is pinned in FOUR places that must move together:
#   docker-compose.yml, .github/workflows/ci.yml, .github/workflows/nightly.yml,
#   scripts/bootstrap.sh
#   -> paradedb/paradedb@sha256:e4e80f2408e556e84b62d18cda7f6bdd690f2939e109e1b132b49e934193f4ed
#      (PostgreSQL 18.6 + pgvector 0.8.4 + pg_search 0.25.7)
#
# Every `pace ...` line below is a real command in provenance/cli.py:
#   demo | ingest | scope | eval {run,fixtures,invariants} | leakage | health | version
# The old `pace eval --suite retrieval --frozen-embeddings ...` and
# `pace leakage --golden eval/golden/golden_set.jsonl` named a sub-command, two flags
# and a file that have never existed.

COMPOSE := docker compose
UV      := uv run
GOLDEN  := provenance/eval/golden/ci_subset.jsonl

.DEFAULT_GOAL := help
.PHONY: help up down db-up db-down trace-up trace-down migrate migrate-down \
        skeleton serve health eval-retrieval eval-fixtures eval-invariants \
        leakage-probe fmt lint typecheck test test-unit test-db

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## --- containers -------------------------------------------------------------

up: ## Start everything (pace-db + jaeger v2), waiting for the real healthchecks
	$(COMPOSE) up -d --wait

down: ## Stop both containers. The named volume pace-pgdata survives.
	$(COMPOSE) stop

db-up: ## Start Postgres 18.6 + pgvector 0.8.4 + pg_search 0.25.7 (pinned by digest)
	$(COMPOSE) up -d --wait pace-db
	@echo "pace-db on localhost:5432 (postgres/pace, db=pace)"

db-down: ## Stop and remove the database container (named volume survives)
	$(COMPOSE) rm -sf pace-db

trace-up: ## Jaeger v2; UI on :16686, OTLP/HTTP on :4318 (matches PACE_OTEL_ENDPOINT)
	$(COMPOSE) up -d --wait jaeger
	@echo "Jaeger UI: http://localhost:16686"

trace-down: ## Stop and remove Jaeger
	$(COMPOSE) rm -sf jaeger

## --- migrations -------------------------------------------------------------

migrate: ## Apply all Alembic revisions (raw SQL, no autogenerate)
	$(UV) alembic upgrade head

migrate-down: ## Roll back one revision
	$(UV) alembic downgrade -1

## --- pipeline ---------------------------------------------------------------

skeleton: ## Walking skeleton: one request end-to-end, one trace, trace_id in the body
	$(UV) pace demo

serve: ## API on :8000. There is no `pace serve`; uvicorn is the server.
	$(UV) uvicorn provenance.api.main:app --reload --port 8000

health: ## Database + extension healthcheck; non-zero if unmigrated or degraded
	$(UV) pace health

## --- evaluation -------------------------------------------------------------

# `--embeddings none` until eval/fixtures/qvecs_<model>.npz is baked and committed; then
# it becomes `--embeddings frozen`. The option takes ONLY none|frozen -- there is no live
# mode anywhere on this path, which is what keeps the gate at exactly $0.00.
eval-retrieval: ## Retrieval gate, exactly as CI runs it. ZERO LLM, ZERO embedding calls.
	$(UV) pace eval run --golden $(GOLDEN) --embeddings none --k 10

eval-fixtures: ## Which golden questions have no committed frozen vector (reports only)
	$(UV) pace eval fixtures --golden $(GOLDEN)

eval-invariants: ## Non-metric invariant checks against the live database
	$(UV) pace eval invariants

leakage-probe: ## Four cheap baselines over the golden set. grep must read < 0.35 recall@10.
	$(UV) pace leakage --from-db

## --- quality ----------------------------------------------------------------

fmt: ## Format and autofix
	$(UV) ruff format .
	$(UV) ruff check --fix .

lint: ## Lint without fixing (what CI runs)
	$(UV) ruff check .
	$(UV) ruff format --check .

typecheck:
	$(UV) mypy provenance

test: ## Everything. db-marked tests skip cleanly when pace-db is not up.
	$(UV) pytest

test-unit: ## The fast half: no database, no containers, runs anywhere
	$(UV) pytest tests/unit -q -m "not db"

test-db: ## Only the tests that need pace-db
	$(UV) pytest -q -m db
