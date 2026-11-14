"""Model access through an OpenAI-compatible chat endpoint (Open WebUI)."""

import httpx
import pytest

from llm import LLM, LLMUnavailable


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ask_sends_model_messages_and_key_and_returns_text():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"action":"done"}'}}]})

    llm = LLM("https://ai.example/", "deepseek-chat", "sk-test", client=client(handler))
    assert llm.ask("system text", "user text") == '{"action":"done"}'
    assert seen["url"] == "https://ai.example/api/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "deepseek-chat" and seen["body"]["stream"] is False
    assert seen["body"]["messages"] == [{"role": "system", "content": "system text"}, {"role": "user", "content": "user text"}]


@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"detail": "bad key"}), httpx.Response(502, text="bad gateway"),
    httpx.Response(200, text="<html>login</html>"), httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"message": {"content": None}}]})])
def test_bad_answers_mean_unavailable(response):
    with pytest.raises(LLMUnavailable):
        LLM("https://ai.example", "m", "k", client=client(lambda request: response)).ask("s", "u")


def test_network_error_means_unavailable():
    def handler(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(LLMUnavailable):
        LLM("https://ai.example", "m", "k", client=client(handler)).ask("s", "u")


def test_error_text_never_contains_the_key():
    llm = LLM("https://ai.example", "m", "sk-secret", client=client(lambda r: httpx.Response(401, text="sk-secret is wrong")))
    with pytest.raises(LLMUnavailable) as e:
        llm.ask("s", "u")
    assert "sk-secret" not in str(e.value)
