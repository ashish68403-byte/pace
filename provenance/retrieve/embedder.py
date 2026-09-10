"""Embedding backends. THE CANONICAL FROZEN-EMBEDDING IMPLEMENTATION LIVES HERE.

One protocol, three implementations, selected by settings:

    LocalEmbedder   fastembed / ONNX, bge-small-en-v1.5, 384 dims. THE DEFAULT.
                    Loads in seconds on CPU, no torch, no CUDA wheels.
    FrozenEmbedder  reads vectors out of a committed .npz keyed by
                    sha256(model_id + "\\x00" + text). RAISES on a miss.
                    THIS IS THE CI PATH.
    ApiEmbedder     thin hosted client, imported lazily, for the (unlikely) day
                    a bigger model is worth the money.

--------------------------------------------------------------------------
ONE CACHE FORMAT. THIS MODULE OWNS IT.
--------------------------------------------------------------------------
`provenance/eval/runner.py` and `scripts/leakage_probe.py` MUST import
`embedding_key`, `FrozenEmbeddingMiss`, `FrozenEmbedder`, `query_text` and
`FIXTURE_PATH_TEMPLATE` from here and keep no parallel implementation. The tree
previously carried three mutually unreadable .npz formats -- one keyed on the
prefixed query, one on the raw question, one as four parallel arrays -- so the
CI gate and the leakage probe could never share a file, and in practice one of
them was always missing rather than merely stale.

The format is a FLAT mapping {embedding_key(model_id, text): float32[dim]}, and
the text that is hashed is the FINAL text handed to the model, i.e. AFTER the
instruction prefix `query_text()` adds. Hash the raw question instead and the
cache stops being a stand-in for the live embedder: every frozen vector would be
keyed by a string the model never saw.

THE FAILURE THIS MODULE PREVENTS: a CI job that silently starts costing money.
FrozenEmbedder never falls back to a live call. If the frozen cache does not
contain a query, that is a bug in the cache-build step and CI must go red at
that exact point -- not quietly dial an API for every eval query on every push,
forever, until someone reads a bill. The miss is a hard error by construction:
there is no fallback branch to accidentally take.

Model identity is `settings.embedding_model_version`, and it is a COLUMN on the
EMBEDDING row (part of its primary key), never part of chunk_id. Re-embedding
with a new model must not invalidate chunk ids (and therefore the golden set);
it must only invalidate the vectors. See provenance/graph/ids.py.

--------------------------------------------------------------------------
BATCH EMBEDDING BELONGS ON KAGGLE, NOT ON THIS LAPTOP
--------------------------------------------------------------------------
Embedding the whole airflow corpus on 2 CPU cores is measured in hours. Kaggle
gives ~30 free GPU hours/week and that is the right place for it. The pipeline
is deliberately a plain file handoff -- no cluster, no queue:

    1. local:   pace ingest chunk  ->  data/export/chunks.parquet
                columns: chunk_id (text, the join key), content (text)
    2. kaggle:  upload chunks.parquet as a dataset; in a GPU notebook run the
                same model at a large batch size; write embeddings.parquet
                columns: chunk_id (text), embedding (list<float32>[384])
    3. local:   download embeddings.parquet
    4. local:   COPY it into the IDLE colour of the blue/green pair (there is no
                embedding column on `chunks` -- vectors live in
                embeddings_code_blue/_green, 0004_vectors), then flip the
                `code_current` alias in index_aliases. Two UPDATEs, one
                transaction, no downtime, no code change.

The chunk_id is content-addressed, so it is stable across the round trip and
needs no special-casing: no row ids to remap, no ordering to preserve, no
join-key column to invent. If a chunk changed while the GPU job was running, its
id changed too and the UPDATE simply does not match -- a stale embedding cannot
be attached to the wrong content. That property is the whole reason the id is a
content hash.

The model on Kaggle MUST be `settings.embedding_model_version`. Record it in the
parquet metadata and assert on it at COPY time; a silent model mismatch produces
a vector space where nothing is wrong-looking and everything is wrong.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import numpy as np

from provenance.config import settings

__all__ = [
    "BGE_QUERY_PREFIX",
    "FIXTURE_PATH_TEMPLATE",
    "ApiEmbedder",
    "Embedder",
    "FrozenEmbedder",
    "FrozenEmbeddingMiss",
    "LocalEmbedder",
    "embedding_key",
    "fixture_path",
    "get_embedder",
    "query_text",
    "repo_root",
]

# bge-* models want an instruction prefix on the QUERY side only. Documents are
# embedded bare. Getting this asymmetry wrong costs a few points of nDCG and is
# invisible unless you look for it.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

#: Repo-root-relative location of the frozen query-vector cache. ONE path, one
#: format, one key function -- the runner, the fixture baker and the leakage
#: probe all resolve through `fixture_path()` so a renamed directory cannot
#: leave two of them reading different files and both reporting "no misses".
FIXTURE_PATH_TEMPLATE: Final = "eval/fixtures/qvecs_{model_id}.npz"


def embedding_key(model_id: str, text: str) -> str:
    """Cache key for a frozen embedding: sha256(model_id + NUL + text).

    The model id is inside the key so two models can share one .npz without
    colliding, and so a model bump is a miss (loud) rather than a silent reuse
    of vectors from a different space.

    ``text`` is the FINAL text the model receives. For queries that means
    `query_text(question)`, not the bare question -- see the module docstring.
    """
    return hashlib.sha256(f"{model_id}\x00{text}".encode()).hexdigest()


def query_text(text: str) -> str:
    """The exact string a query is embedded as, prefix included.

    Exported so the fixture baker, the runner and the leakage probe can compute
    a key without re-typing the prefix. A prefix that drifts in one caller is a
    100% miss rate in that caller and a green run everywhere else.
    """
    return BGE_QUERY_PREFIX + text


def repo_root() -> Path:
    """Nearest ancestor containing pyproject.toml; cwd if there is none."""
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return Path.cwd()


def fixture_path(model_id: str | None = None, eval_root: Path | str | None = None) -> Path:
    """Absolute path of the frozen query-vector cache for ``model_id``.

    ``PACE_EVAL_ROOT`` (the eval directory, as used by the eval runner) wins
    over the repo-relative template, so a CI job can point every consumer at one
    directory without any of them hardcoding a second layout.
    """
    model = model_id or settings.embedding_model_version
    root = eval_root or os.environ.get("PACE_EVAL_ROOT")
    if root:
        return (Path(root) / "fixtures" / f"qvecs_{model}.npz").resolve()
    return (repo_root() / FIXTURE_PATH_TEMPLATE.format(model_id=model)).resolve()


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into unit-norm float32 vectors of embedding_dim."""

    model_id: str
    dim: int

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        """(n, dim) float32, L2-normalised."""
        ...

    def encode_query(self, text: str) -> np.ndarray:
        """(dim,) float32, L2-normalised."""
        ...


def _l2_normalise(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m.reshape(1, -1)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    # A zero vector would divide by zero; it also cannot be meaningfully
    # normalised, so leave it at zero rather than producing NaNs that only
    # surface as a confusing pgvector error 40 minutes into an ingest.
    np.maximum(norms, 1e-12, out=norms)
    normalised: np.ndarray = (m / norms).astype(np.float32)
    return normalised


class LocalEmbedder:
    """fastembed / ONNX Runtime on CPU. The default backend.

    fastembed is chosen over sentence-transformers precisely because it does not
    pull torch. On Linux the torch wheel drags ~1.5-2 GB of CUDA packages onto a
    machine that has no CUDA. fastembed ships ONNX and runs bge-small-en-v1.5 in
    a couple of hundred MB.

    Import is lazy: `pace` must be importable (CLI --help, unit tests, the CI
    retrieval gate) on an install that never took the "ml" extra.
    """

    def __init__(self, model_id: str | None = None, dim: int | None = None) -> None:
        self.model_id = model_id or settings.embedding_model_version
        self.dim = dim or settings.embedding_dim
        self._model: Any = None
        self._lock = threading.Lock()

    def _ensure(self) -> Any:  # fastembed's TextEmbedding is not importable at rest
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from fastembed import TextEmbedding
                    except ImportError as exc:  # pragma: no cover - env-dependent
                        # Name the package, not just the extra. `fastembed` was
                        # imported here while being declared nowhere in
                        # pyproject.toml, so `uv sync --extra ml` installed
                        # sentence-transformers/onnxruntime/optimum and left THE
                        # DEFAULT BACKEND raising anyway -- an error message
                        # that says "install the ml extra" when the ml extra is
                        # already installed sends you looking in the wrong file.
                        raise RuntimeError(
                            "LocalEmbedder requires `fastembed` (module not importable). "
                            "It must be declared in pyproject.toml under "
                            "[project.optional-dependencies] ml, then installed with "
                            "`uv sync --extra ml`. If `uv sync --extra ml` has already "
                            "been run, the extra is missing the fastembed entry."
                        ) from exc
                    # 2 physical cores. More threads than that is contention.
                    self._model = TextEmbedding(model_name=f"BAAI/{self.model_id}", threads=2)
        return self._model

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = list(self._ensure().embed(list(texts), batch_size=16))
        return _l2_normalise(np.vstack(vecs))

    def encode_query(self, text: str) -> np.ndarray:
        # query_text(), not a second copy of the prefix: the frozen cache is
        # keyed on exactly this string, so the two must be one function.
        vecs = list(self._ensure().query_embed([query_text(text)]))
        row: np.ndarray = _l2_normalise(np.asarray(vecs[0]))[0]
        return row


class FrozenEmbeddingMiss(RuntimeError):
    """A query was not in the committed frozen-embedding cache.

    Deliberately fatal. See the module docstring: the alternative is a CI job
    that quietly makes network calls.
    """


class FrozenEmbedder:
    """Reads vectors from a committed .npz. Never calls a model. Never calls out.

    The .npz maps embedding_key(model_id, text) -> float32[dim]. It is built by
    a separate, explicitly-run offline step (`pace eval fixtures` reports what
    is missing) and committed, so the CI retrieval gate is pure: zero LLM calls, zero embedding
    API calls, byte-identical vectors on every run. That last property is also
    what makes the T5 determinism invariant checkable at all -- with a live
    model, "same query, same ranking" would be a claim about a model server's
    mood.
    """

    def __init__(self, path: Path | str | None = None, model_id: str | None = None) -> None:
        self.model_id = model_id or settings.embedding_model_version
        # Default through fixture_path() so every consumer -- runner, probe,
        # fixture baker -- lands on the same file without repeating the layout.
        self.path = Path(path) if path is not None else fixture_path(self.model_id)
        if not self.path.exists():
            raise FrozenEmbeddingMiss(
                f"frozen embedding cache missing: {self.path}. Bake it offline "
                "(`pace eval fixtures` lists the questions with no vector) and "
                "commit it. Not falling back to a live embedding call."
            )
        with np.load(self.path) as npz:
            self._vectors: dict[str, np.ndarray] = {
                k: np.asarray(npz[k], dtype=np.float32) for k in npz.files
            }
        dims = {v.shape[-1] for v in self._vectors.values()}
        if len(dims) > 1:
            raise ValueError(f"frozen cache {self.path} mixes dims: {sorted(dims)}")
        self.dim = dims.pop() if dims else settings.embedding_dim
        if self.dim != settings.embedding_dim:
            raise ValueError(
                f"frozen cache dim {self.dim} != settings.embedding_dim "
                f"{settings.embedding_dim}; the cache was built with a different model"
            )

    def __len__(self) -> int:
        return len(self._vectors)

    def has(self, text: str) -> bool:
        """Is this EXACT text (prefix included) in the cache?"""
        return embedding_key(self.model_id, text) in self._vectors

    def has_query(self, question: str) -> bool:
        """Is this question's query vector in the cache?

        For pre-flighting a whole golden set before scoring starts, so a miss is
        reported once as a list of question ids rather than as a crash 40
        questions into a run.
        """
        return self.has(query_text(question))

    def _lookup(self, text: str) -> np.ndarray:
        key = embedding_key(self.model_id, text)
        try:
            return self._vectors[key]
        except KeyError:
            # No fallback branch here, on purpose. Adding one is how CI starts
            # spending money without anyone noticing.
            raise FrozenEmbeddingMiss(
                f"no frozen embedding for key {key[:12]}... "
                f"(model={self.model_id!r}, text={text[:80]!r}). "
                "Bake the missing vectors offline (`pace eval fixtures` lists them) "
                "and commit the .npz. "
                "This is NOT falling back to a live embedding call."
            ) from None

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _l2_normalise(np.vstack([self._lookup(t) for t in texts]))

    def encode_query(self, text: str) -> np.ndarray:
        # Freeze the PREFIXED string: what is cached must be exactly what a live
        # LocalEmbedder would have been handed, or the cache is not a stand-in.
        row: np.ndarray = _l2_normalise(self._lookup(query_text(text)))[0]
        return row


class ApiEmbedder:
    """Thin hosted-embedding client. Lazily imported, not the default.

    Kept small on purpose: it exists so the interface is proven to be
    provider-shaped, not so the project depends on a vendor. Any use of this in
    a code path that CI executes is a bug -- CI uses FrozenEmbedder.
    """

    def __init__(
        self,
        model_id: str,
        dim: int,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
    ) -> None:
        self.model_id = model_id
        self.dim = dim
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client: Any = None

    def _http(self) -> Any:  # httpx.Client, imported lazily
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
        return self._client

    def _embed(self, texts: Sequence[str]) -> np.ndarray:
        resp = self._http().post("/embeddings", json={"model": self.model_id, "input": list(texts)})
        resp.raise_for_status()
        data = resp.json()["data"]
        # Providers do not guarantee response order; sort by the echoed index.
        data.sort(key=lambda d: d["index"])
        return _l2_normalise(np.asarray([d["embedding"] for d in data], dtype=np.float32))

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out = [self._embed(texts[i : i + 64]) for i in range(0, len(texts), 64)]
        return np.vstack(out)

    def encode_query(self, text: str) -> np.ndarray:
        row: np.ndarray = self._embed([text])[0]
        return row


def _configured(field: str, env: str) -> str | None:
    """Read a knob from Settings, falling back to the raw environment.

    `Settings` uses `extra="ignore"`, so an undeclared PACE_* variable is
    dropped without a word: the DOCUMENTED `PACE_EMBEDDING_BACKEND` override was
    silently a no-op, and the only symptom was that the backend you asked for
    was not the backend you got. Reading os.environ here is a bridge, not the
    fix -- config.py must declare `embedding_backend` and
    `frozen_embeddings_path`, at which point this falls through to settings and
    the env var is validated like every other knob.

    This cannot turn the CI path into a live path: an explicit `backend=`
    argument still wins over both, and that is what CI passes.
    """
    value = getattr(settings, field, None)
    if value is not None:
        return str(value)
    return os.environ.get(env)


def get_embedder(backend: str | None = None, frozen_path: Path | str | None = None) -> Embedder:
    """Select a backend. Default: local.

    ``backend`` overrides everything; otherwise PACE_EMBEDDING_BACKEND, then
    "local". CI sets backend="frozen" explicitly rather than relying on an env
    var being present, because a missing env var must not be able to turn the CI
    path into a live path.
    """
    name = (
        backend or _configured("embedding_backend", "PACE_EMBEDDING_BACKEND") or "local"
    ).lower()
    if name == "local":
        return LocalEmbedder()
    if name == "frozen":
        path = frozen_path or _configured("frozen_embeddings_path", "PACE_FROZEN_EMBEDDINGS_PATH")
        # None -> FrozenEmbedder resolves fixture_path() itself, which is the
        # one canonical location. Do not reintroduce a second default here.
        return FrozenEmbedder(path)
    if name == "api":
        import os

        return ApiEmbedder(
            model_id=settings.embedding_model_version,
            dim=settings.embedding_dim,
            base_url=os.environ["PACE_EMBEDDING_API_BASE"],
            api_key=os.environ["PACE_EMBEDDING_API_KEY"],
        )
    raise ValueError(f"unknown embedding backend {name!r}; expected local|frozen|api")
