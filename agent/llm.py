"""Model access through an OpenAI-compatible chat endpoint (Open WebUI: <base>/api/chat/completions)."""

import httpx


class LLMUnavailable(Exception):
    """The model endpoint did not give a usable answer; the case must not count as an attempt."""


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 60, client: httpx.Client | None = None):
        self._url = base_url.rstrip("/") + "/api/chat/completions"
        self._model = model
        self._key = api_key
        self._timeout = timeout
        self._client = client or httpx.Client()

    def ask(self, system: str, user: str) -> str:
        body = {
            "model": self._model, "stream": False, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            r = self._client.post(self._url, json=body, headers={"Authorization": f"Bearer {self._key}"},
                                  timeout=self._timeout)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:     # InvalidURL is no HTTPError
            raise LLMUnavailable(f"model endpoint not reachable: {type(e).__name__}") from None
        if r.status_code != 200:
            raise LLMUnavailable(f"model endpoint answered {r.status_code}")
        try:
            text = r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMUnavailable("model endpoint answered without a message") from None
        if not isinstance(text, str):
            raise LLMUnavailable("model endpoint answered without text")
        return text
