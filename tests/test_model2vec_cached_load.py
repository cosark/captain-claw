"""model2vec 0.8 defaults ``from_pretrained(force_download=True)``, so loading
the in-process embedding model re-checked every file with the Hugging Face Hub
even when it was cached. A slow, rate-limited Hub stretched an agent's first
load to 1m41s; the first turn waited on the loader's lock on the event loop, and
every websocket handshake to the agent timed out meanwhile. The cache is used
now, and the Hub is only asked when the model isn't there.
"""

from __future__ import annotations

import sys
import types

from captain_claw.semantic_memory import _Model2VecEmbeddingProvider


def _fake_model2vec(monkeypatch, from_pretrained):
    static_model = type("StaticModel", (), {"from_pretrained": staticmethod(from_pretrained)})
    monkeypatch.setitem(sys.modules, "model2vec", types.SimpleNamespace(StaticModel=static_model))


def test_a_cached_model_is_loaded_without_asking_the_hub(monkeypatch):
    calls: list[tuple] = []

    def from_pretrained(path, token=None, force_download=True):
        calls.append((path, force_download))
        return "encoder"

    _fake_model2vec(monkeypatch, from_pretrained)

    provider = _Model2VecEmbeddingProvider("minishlab/potion-base-8M")
    assert provider._ensure_encoder() == "encoder"
    assert provider._ensure_encoder() == "encoder"  # loaded once
    assert calls == [("minishlab/potion-base-8M", False)]


def test_an_older_model2vec_without_the_flag_still_loads(monkeypatch):
    calls: list[str] = []

    def from_pretrained(path, token=None):
        calls.append(path)
        return "encoder"

    _fake_model2vec(monkeypatch, from_pretrained)

    assert _Model2VecEmbeddingProvider("m")._ensure_encoder() == "encoder"
    assert calls == ["m"]
