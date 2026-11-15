"""The HTTP service: answers for broken cases and bodies, model configuration, and what it logs.
Needs Playwright and Flask for the import: run with tests/run-in-docker.sh."""

import importlib
import logging

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("flask")

ENV = {"LLM_BASE_URL": "https://ai.example", "LLM_MODEL": "deepseek-chat", "LLM_API_KEY": "sk-test"}
KEYS = ["evidence", "evidence_url", "reason", "result", "stage", "steps"]
URL = "https://shop.example/u?token=secret"
ADDRESS = "anne@wagnersnetz.de"


@pytest.fixture
def server(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    import server as module
    return importlib.reload(module)


@pytest.fixture
def client(server):
    return server.app.test_client()


def result(reason, result="unsubscribed"):
    return {"result": result, "reason": reason, "evidence": "Sie wurden abgemeldet", "evidence_url": URL,
            "steps": 2, "stage": 2}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.get_json() == {"status": "ok"}


@pytest.mark.parametrize("body", ["not json", "[1, 2]", "null", "{}", '{"url": 1}', '{"url": "https://x.example"'
                                  ', "address": 5}', '"https://x.example"'])
def test_bad_bodies_answer_400(client, body):
    assert client.post("/run", data=body, content_type="application/json").status_code == 400


def test_exception_in_a_case_is_unclear_and_logs_nothing_personal(server, client, monkeypatch, caplog):
    def boom(*args, **kw):
        raise RuntimeError(f"{URL} {ADDRESS}")

    monkeypatch.setattr(server, "run_case", boom)
    caplog.set_level(logging.DEBUG)
    r = client.post("/run", json={"url": URL, "address": ADDRESS})
    assert r.status_code == 200
    body = r.get_json()
    assert sorted(body) == KEYS
    assert (body["result"], body["reason"], body["steps"], body["stage"]) == ("unclear", "agent_failure", 0, 1)
    assert "RuntimeError" in caplog.text
    for secret in ("shop.example", "token", "secret", ADDRESS, "anne"):
        assert secret not in caplog.text


def test_browser_launch_failure_is_unclear_too(server, client, monkeypatch, caplog):
    def no_browser():
        raise OSError(f"cannot start browser for {URL}")

    monkeypatch.setattr(server, "sync_playwright", no_browser)
    caplog.set_level(logging.DEBUG)
    body = client.post("/run", json={"url": URL, "address": ADDRESS}).get_json()
    assert (body["result"], body["reason"]) == ("unclear", "agent_failure")
    assert "shop.example" not in caplog.text and "OSError" in caplog.text


def test_model_reason_is_answered_but_logged_only_as_a_category(server, client, monkeypatch, caplog):
    reason = "unsubscribed anne@wagnersnetz.de at https://x.example/u?t=1"
    monkeypatch.setattr(server, "run_case", lambda *args, **kw: result(reason))
    caplog.set_level(logging.DEBUG)
    body = client.post("/run", json={"url": URL, "address": ADDRESS}).get_json()
    assert body["reason"] == reason and body["evidence_url"] == URL
    assert "anne" not in caplog.text and "x.example" not in caplog.text and "shop.example" not in caplog.text
    assert "case finished: unsubscribed (model), 2 step(s), stage 2" in caplog.text


@pytest.mark.parametrize("reason", ["login", "captcha", "blocked_address", "budget", "page_error", "llm_unavailable",
                                    "page confirms without interaction",
                                    "three invalid replies from the model in a row"])
def test_reasons_set_by_code_are_logged_as_they_are(server, client, monkeypatch, caplog, reason):
    monkeypatch.setattr(server, "run_case", lambda *args, **kw: result(reason, "not_possible"))
    caplog.set_level(logging.INFO)
    client.post("/run", json={"url": URL, "address": ADDRESS})
    assert f"case finished: not_possible ({reason})" in caplog.text


def test_case_gets_the_configured_model_and_limits(server, client, monkeypatch):
    seen = {}

    def fake_run_case(browser, url, address, llm, **kw):
        seen.update(url=url, address=address, llm=llm, **kw)
        return result("budget", "not_possible")

    monkeypatch.setattr(server, "run_case", fake_run_case)
    client.post("/run", json={"url": URL, "address": ADDRESS})
    assert (seen["url"], seen["address"]) == (URL, ADDRESS)
    assert seen["max_steps"] == server.MAX_STEPS and seen["case_timeout"] == server.CASE_TIMEOUT
    assert seen["llm"]._model == "deepseek-chat"


@pytest.mark.parametrize("missing", sorted(ENV))
@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_model_configuration_stops_the_start(monkeypatch, missing, value):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    import server as module
    if value is None:
        monkeypatch.delenv(missing)
    else:
        monkeypatch.setenv(missing, value)
    try:
        with pytest.raises(RuntimeError, match=missing):
            importlib.reload(module)
    finally:
        monkeypatch.setenv(missing, ENV[missing])
        importlib.reload(module)
