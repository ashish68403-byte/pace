"""Single source of truth for every runtime knob in PACE.

One module owns configuration so that no other module ever re-reads os.environ
or hard-codes a connection string. That matters more than usual here: the
embedding model version and the chunking strategy version are written into
database columns as provenance of derivation, and an eval run is only
interpretable if the value that produced a row is the value this object held.
Two modules disagreeing about `embedding_model_version` would produce a corpus
whose rows silently mix two derivations, and no query could tell them apart.

Every field is overridable from the environment with the PACE_ prefix, e.g.
PACE_DATABASE_URL, PACE_CORPUS_PATH.

The corollary of `extra="ignore"` below: a PACE_ variable with no matching field
here is dropped WITHOUT A WORD. That is deliberate -- an unrelated PACE_* in a
shared shell must not crash the process -- but it means every documented
override has to have a real field, or the documentation is a lie that produces
no error message. `PACE_EMBEDDING_BACKEND` was exactly that: documented,
consumed via getattr with a default, and silently inert. If you document a knob,
declare it here.
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, populated from the environment and .env."""

    model_config = SettingsConfigDict(
        env_prefix="PACE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Local container: paradedb (PostgreSQL 18.6 + pgvector 0.8.4 + pg_search 0.25.7).
    database_url: str = "postgresql://postgres:pace@localhost:5432/pace"

    # The airflow clone lives outside the repo; its text is not ours to commit.
    corpus_path: Path = Path.home() / "corpus" / "airflow"

    repo_owner: str = "apache"
    repo_name: str = "airflow"

    # Walking skeleton uses a small local model; 384 dims keeps the index small
    # enough to build on 2 cores with no CUDA.
    embedding_dim: int = 384
    embedding_model_version: str = "bge-small-en-v1.5"

    # Which Embedder `provenance.retrieve.embedder.get_embedder()` returns when
    # no explicit backend argument is given:
    #   local   fastembed / ONNX on CPU (the default; needs the "ml" extra)
    #   frozen  read-only lookup in a committed .npz, RAISES on a miss
    #   api     hosted provider; never on a CI path
    # CI passes backend="frozen" explicitly rather than relying on this, so that
    # a missing environment variable can never turn the gate into a live,
    # billable path. This field only decides the DEFAULT.
    embedding_backend: str = "local"

    # None means "use the canonical location", i.e.
    # embedder.FIXTURE_PATH_TEMPLATE -> eval/fixtures/qvecs_<model_id>.npz.
    # Do NOT give this a concrete default: a second default path is how the
    # runner and the leakage probe ended up reading two different .npz files
    # and each finding the other's empty (audit #16).
    frozen_embeddings_path: Path | None = None

    # Derivation provenance. These are COLUMNS, never inputs to chunk_id -
    # see provenance/graph/ids.py for why folding them into the hash would
    # invalidate every golden-set anchor on the first chunker change.
    chunking_strategy_version: str = "v1"

    otel_endpoint: str = "http://localhost:4318"
    service_name: str = "pace"

    @field_validator("embedding_backend")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        """Reject a typo at startup instead of at the first embed call.

        Without this, `PACE_EMBEDDING_BACKEND=frozn` is accepted here and only
        surfaces much later, from get_embedder(), after a fixture load and
        possibly a scoring loop have already run.
        """
        normalised = value.strip().lower()
        allowed = {"local", "frozen", "api"}
        if normalised not in allowed:
            raise ValueError(f"embedding_backend must be one of {sorted(allowed)}, got {value!r}")
        return normalised


settings = Settings()
