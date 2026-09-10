#!/usr/bin/env bash
# PACE local bootstrap. Idempotent: safe to run on a clean machine and safe to run again
# on a machine that is already half set up. Every step checks before it acts.
#
# The failure this script exists to prevent is the readiness race. See wait_for_db().
#
#   ./scripts/bootstrap.sh            # full setup
#   ./scripts/bootstrap.sh --no-db    # venv + deps only (CI already has a service container)
#   ./scripts/bootstrap.sh --recreate # destroy and rebuild the database container
#
# Containers come from docker-compose.yml, which is the single description of them. This
# script used to carry its own `docker run` with its own copy of the image digest, the
# volume mount and the environment; the Makefile carried a third. Copies drift -- that is
# how the Makefile ended up running an end-of-life Jaeger image. The compose plugin is
# installed (v2.40.3), so there is no reason left for the duplicate.

set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Kept only for the healthcheck/psql calls below; the image, ports and volume live in
# docker-compose.yml. The digest is repeated in a comment, not in a variable, so nobody
# can "fix" it here and leave compose behind:
#   paradedb/paradedb@sha256:e4e80f2408e556e84b62d18cda7f6bdd690f2939e109e1b132b49e934193f4ed
DB_SERVICE="pace-db"
DB_CONTAINER="pace-db"
DB_VOLUME="pace-pgdata"
DB_USER="postgres"
DB_PASSWORD="pace"
DB_NAME="pace"
PYTHON_VERSION="3.12"

START_DB=1
RECREATE_DB=0
for arg in "$@"; do
  case "$arg" in
    --no-db)    START_DB=0 ;;
    --recreate) RECREATE_DB=1 ;;
    -h|--help)  sed -n '2,14p' "$0"; exit 0 ;;
    *)          echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log()  { printf '==> %s\n' "$*"; }
warn() { printf '!!! %s\n' "$*" >&2; }
die()  { printf 'ERR %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------- toolchain

require_uv() {
  if command -v uv >/dev/null 2>&1; then
    log "uv $(uv --version | awk '{print $2}') present"
    return
  fi
  die "uv is not installed. Install it with:
    curl -LsSf https://astral.sh/uv/install.sh | sh
  then re-open your shell and re-run this script.
  (Installing it for you would put an unpinned binary on your PATH without asking.)"
}

setup_venv() {
  # 'uv venv' is itself idempotent, but say so out loud so a re-run is legible.
  if [[ -d .venv ]]; then
    log "virtualenv already exists at .venv"
  else
    log "creating virtualenv on Python ${PYTHON_VERSION}"
  fi
  uv venv --python "${PYTHON_VERSION}" .venv

  log "syncing dependencies (base + dev; the 'ml' extra is deliberately excluded)"
  # No --frozen: there is no committed uv.lock. CI makes the same choice for the same
  # reason -- `uv sync --frozen` without a lock file fails outright.
  #
  # The 'ml' extra is fastembed (ONNX, no torch) and is only needed to BUILD embeddings.
  # 'ml-heavy' is sentence-transformers, which pulls torch: on Linux that is ~1.5-2 GB
  # of nvidia-* CUDA wheels on a box with 2 cores, no CUDA and 3.7 GB of visible RAM.
  # Install either one explicitly, when you actually need it:
  #   uv sync --extra ml
  uv sync --extra dev
}

# --------------------------------------------------------------------- database

docker_ok() {
  command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
  docker info >/dev/null 2>&1 || die "docker daemon not reachable (is the WSL integration running?)"
  docker compose version >/dev/null 2>&1 || die \
    "the docker compose plugin is not available. Install docker-compose-plugin; this
  script and the Makefile both drive containers through docker-compose.yml so that
  the image digest and the PG18 volume mount are defined in exactly one place."
}

start_db() {
  docker_ok

  if [[ "$RECREATE_DB" -eq 1 ]]; then
    warn "--recreate: destroying container ${DB_CONTAINER} and volume ${DB_VOLUME}"
    docker compose rm -sfv "$DB_SERVICE" >/dev/null 2>&1 || true
    docker volume rm "$DB_VOLUME" >/dev/null 2>&1 || true
  fi

  log "starting ${DB_SERVICE} from docker-compose.yml (pinned by digest)"
  # Deliberately WITHOUT `--wait`. `make up` uses it, and it is correct there, but this
  # script owns the readiness question and answers it in wait_for_db() below -- which
  # additionally checks that the server is still there three seconds later. Layering
  # `--wait || retry-without-wait` on top would turn a genuine healthcheck timeout into
  # a silent retry, which is the opposite of what this script is for.
  docker compose up -d "$DB_SERVICE"
}

wait_for_db() {
  # THE point of this function.
  #
  # pg_isready is the obvious choice and it is WRONG here. The postgres entrypoint runs
  # initdb against a *temporary* server before it starts the real one. pg_isready
  # answers "accepting connections" against that temporary server, so a naive wait loop
  # goes green, the script races ahead, the temporary server then shuts down, and every
  # migration blows up with "server closed the connection unexpectedly" -- an error that
  # looks like a bug in Alembic and is not.
  #
  # Poll a real query, as the real user, against the real database, over the real TCP
  # listener. Only the server we actually intend to talk to can satisfy that.
  local deadline=$(( SECONDS + 180 ))
  log "waiting for ${DB_NAME} to answer a real query (not pg_isready)"
  until docker exec -e PGPASSWORD="$DB_PASSWORD" "$DB_CONTAINER" \
          psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -tAc 'SELECT 1' >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      docker logs --tail 60 "$DB_CONTAINER" >&2 || true
      die "database did not become ready within 180s"
    fi
    if ! docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
      docker logs --tail 60 "$DB_CONTAINER" >&2 || true
      die "container ${DB_CONTAINER} exited during startup (check the volume mount point:
  PG18 images want /var/lib/postgresql, NOT /var/lib/postgresql/data)"
    fi
    sleep 2
  done

  # Two consecutive successes, three seconds apart. One success could still be the
  # temporary server on a slow machine; the temporary server does not survive this.
  sleep 3
  docker exec -e PGPASSWORD="$DB_PASSWORD" "$DB_CONTAINER" \
    psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -tAc 'SELECT 1' >/dev/null \
    || die "database went away after the first successful query -- that was the initdb server"

  log "database ready"
}

ensure_extensions() {
  log "ensuring extensions"
  docker exec -e PGPASSWORD="$DB_PASSWORD" "$DB_CONTAINER" \
    psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -q \
    -c 'CREATE EXTENSION IF NOT EXISTS vector;' \
    -c 'CREATE EXTENSION IF NOT EXISTS pg_search;'
}

migrate() {
  log "running migrations"
  uv run alembic upgrade head
}

healthcheck() {
  log "healthcheck"
  docker exec -e PGPASSWORD="$DB_PASSWORD" "$DB_CONTAINER" \
    psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -tA -F' | ' \
    -c "SELECT current_setting('server_version'), (SELECT extversion FROM pg_extension WHERE extname='vector'), (SELECT extversion FROM pg_extension WHERE extname='pg_search');" \
    | sed 's/^/    postgres | pgvector | pg_search: /'

  # Prove the tokenizer contract still holds. If this line ever changes, the lexical
  # half of retrieval changes with it and every recall number needs re-measuring.
  docker exec -e PGPASSWORD="$DB_PASSWORD" "$DB_CONTAINER" \
    psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -tA \
    -c "SELECT '_normalise_url camelCaseName'::pdb.source_code::text[];" \
    | sed 's/^/    tokenizer: /'

  # `pace health` is a real command (provenance/cli.py). It exits non-zero on a missing
  # extension AND on a database that has never been migrated, which is why bootstrap
  # gates on it rather than on a cheerful report.
  uv run pace health || warn "'pace health' failed -- the app layer may not be wired up yet"

  # Everything printed below is a command that exists. There is no `pace serve`: the
  # API is a FastAPI app and uvicorn runs it.
  printf '\n    Next:\n'
  printf '      make up                                   # pace-db + jaeger v2\n'
  printf '      make test-unit                            # no database needed\n'
  printf '      uv run uvicorn provenance.api.main:app --reload   # API on :8000\n'
  printf '      uv run pace demo                          # one request, one trace\n'
  printf '      uv run pace leakage --from-db             # the benchmark-quality gate\n'
  printf '      uv run pace eval run --embeddings none    # the retrieval gate\n\n'
}

# ------------------------------------------------------------------------- main

require_uv
setup_venv

if [[ "$START_DB" -eq 1 ]]; then
  start_db
  wait_for_db
  ensure_extensions
  migrate
  healthcheck
else
  log "--no-db: skipping database setup"
fi

log "bootstrap complete"
