"""``load_retriever`` must hand back an *instance*, never the class it was named after.

The distinction is invisible right up until something calls the protocol method. A
class satisfies ``hasattr(obj, "retrieve")`` through its own unbound function, so a
loader that tests only for that attribute concludes "already a retriever" and returns
the class object. The first ``retriever.retrieve(question, qvec, k)`` then binds
``question`` to ``self``, ``qvec`` to ``question``, ``k`` to ``qvec``, and dies on a
missing ``k``.

That is how the default spec shipped, and the default is ``NullRetriever`` -- which
means ``pace eval run`` had never scored a single query, on any machine, ever. The
eval harness is the thing Phase 0 exists to establish *before* the agent, so a harness
that cannot complete a run is worth a test of its own.

The three loader paths are covered together because they are one decision: a class and
a factory both need calling, an already-built singleton must not be called twice.
"""

from __future__ import annotations

import pytest

from provenance.eval import runner as runner_mod
from provenance.eval.runner import NullRetriever, load_retriever


def test_class_spec_is_instantiated() -> None:
    retriever = load_retriever("provenance.eval.runner:NullRetriever")
    assert retriever is not NullRetriever, "returned the class, not an instance"
    assert isinstance(retriever, NullRetriever)


def test_loaded_retriever_accepts_the_protocol_signature() -> None:
    """The regression itself: three positional args, as ``_score_records`` calls it."""
    retriever = load_retriever("provenance.eval.runner:NullRetriever")
    assert list(retriever.retrieve("why is this code the way it is?", None, 10)) == []


def test_singleton_spec_is_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    singleton = NullRetriever()
    monkeypatch.setattr(runner_mod, "_probe_singleton", singleton, raising=False)
    assert load_retriever("provenance.eval.runner:_probe_singleton") is singleton


def test_factory_spec_is_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "_probe_factory", NullRetriever, raising=False)
    retriever = load_retriever("provenance.eval.runner:_probe_factory")
    assert isinstance(retriever, NullRetriever)


def test_name_defaults_to_the_attribute_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unnamed:
        def retrieve(self, question: str, qvec: object, k: int) -> list[str]:
            return []

    monkeypatch.setattr(runner_mod, "_probe_unnamed", Unnamed, raising=False)
    assert load_retriever("provenance.eval.runner:_probe_unnamed").name == "_probe_unnamed"


def test_a_spec_without_retrieve_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "_probe_not_a_retriever", 42, raising=False)
    with pytest.raises(Exception, match="does not provide"):
        load_retriever("provenance.eval.runner:_probe_not_a_retriever")
