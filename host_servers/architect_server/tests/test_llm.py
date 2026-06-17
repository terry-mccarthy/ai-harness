"""Unit tests for the OllamaLLM HTTP wire contract (``/api/chat``)."""
from __future__ import annotations

import json

import httpx

from architect_server.llm import OllamaLLM


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def test_chat_posts_to_api_chat_with_messages():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "hello"}, "done": True},
        )

    llm = OllamaLLM(host="http://test.local:11434", model="qwen2.5-coder:7b")
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return real_client(*args, **kwargs)

    httpx.Client = fake_client
    try:
        out = llm.chat(system="you are a reviewer", user="score this")
    finally:
        httpx.Client = real_client

    assert out == "hello"
    assert captured["url"] == "http://test.local:11434/api/chat"
    assert captured["body"]["model"] == "qwen2.5-coder:7b"
    assert captured["body"]["stream"] is False
    msgs = captured["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "you are a reviewer"}
    assert msgs[1] == {"role": "user", "content": "score this"}


def test_chat_options_carry_temperature_and_num_ctx():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": "x"}, "done": True}
        )

    llm = OllamaLLM(
        host="http://test.local:11434", model="m", temperature=0.0, num_ctx=4096
    )
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return real_client(*args, **kwargs)

    httpx.Client = fake_client
    try:
        llm.chat(system="s", user="u")
    finally:
        httpx.Client = real_client

    opts = captured["body"].get("options", {})
    assert opts.get("temperature") == 0.0
    assert opts.get("num_ctx") == 4096
