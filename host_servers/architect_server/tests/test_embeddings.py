"""Unit tests for the OllamaEmbedder HTTP wire contract."""
from __future__ import annotations

import httpx
import pytest

from architect_server.embeddings import OllamaEmbedder


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def test_embed_one_posts_to_api_embed():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = httpx._content.encode_request(json=None, data=None)  # placeholder
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})

    embedder = OllamaEmbedder(host="http://test.local:11434", model="nomic-embed-text")
    # Monkey-patch httpx.Client to inject the mock transport.
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return real_client(*args, **kwargs)

    httpx.Client = fake_client
    try:
        vec = embedder.embed_one("hello world")
    finally:
        httpx.Client = real_client

    assert vec == [0.1, 0.2, 0.3]
    assert captured["url"] == "http://test.local:11434/api/embed"
    assert captured["body"] == {"model": "nomic-embed-text", "input": "hello world"}


def test_embed_many_batches_in_one_call():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"embeddings": [[0.1, 0.0], [0.0, 0.1], [0.1, 0.1]]},
        )

    embedder = OllamaEmbedder(host="http://test.local:11434", model="nomic-embed-text")
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return real_client(*args, **kwargs)

    httpx.Client = fake_client
    try:
        vecs = embedder.embed_many(["a", "b", "c"])
    finally:
        httpx.Client = real_client

    assert len(vecs) == 3
    assert captured["body"]["input"] == ["a", "b", "c"]


def test_embed_many_empty_returns_empty_without_calling_ollama():
    """Don't waste a network call when the input list is empty."""
    embedder = OllamaEmbedder(host="http://unreachable.invalid", model="x")
    assert embedder.embed_many([]) == []
