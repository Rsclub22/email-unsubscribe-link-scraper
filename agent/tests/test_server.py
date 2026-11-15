"""The HTTP service: answers for broken cases and bodies, model configuration, and what it logs.
Needs Playwright and Flask for the import: run with tests/run-in-docker.sh."""

import importlib
import logging
from ipaddress import ip_network

import pytest

import guard

pytest.importorskip("playwright.sync_api")
pytest.importorskip("flask")

ENV = {"LLM_BASE_URL": "https://ai.example", "LLM_MODEL": "deepseek-chat", "LLM_API_KEY": "sk-test",
       "BLOCKED_NETS": "none"}
LOOPBACK_SWITCH = "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK"
KEYS = ["evidence", "evidence_url", "reason", "result", "stage", "steps"]
URL = "https://shop.example/u?token=secret"
ADDRESS = "anne@wagnersnetz.de"


@pytest.fixture(autouse=True)
def keep_guard(monkeypatch):
    """server sets guard.BLOCKED_NETS at import; put the value back after each test."""
    monkeypatch.setattr(guard, "BLOCKED_NETS", guard.BLOCKED_NETS)
    monkeypatch.delenv(LOOPBACK_SWITCH, raising=False)


@pytest.fixture
def server(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    import server as module
    return importlib.reload(module)


def reload_with(monkeypatch, **env):
    """Reload server with ENV changed by env (None removes a variable); afterwards reload it with ENV again."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    import server as module
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return module


@pytest.fixture
def client(server):
    return server.app.test_client()


def result(reason, result="unsubscribed"):
    return {"result": result, "reason": reason, "evidence": "Sie wurden abgemeldet", "evidence_url": URL,
            "steps": 2, "stage": 2}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.get_json() == {"status": "ok"}


def test_health_answers_while_a_case_holds_the_lock(server):
    import threading
    answers = []

    def ask():
        answers.append(server.app.test_client().get("/health").status_code)

    with server.LOCK:                     # a case is running
        t = threading.Thread(target=ask)
        t.start()
        t.join(timeout=5)
    assert answers == [200]


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


def test_without_blocked_nets_the_service_does_not_start(monkeypatch):
    module = reload_with(monkeypatch, BLOCKED_NETS=None)
    try:
        with pytest.raises(RuntimeError, match="BLOCKED_NETS"):
            importlib.reload(module)
    finally:
        monkeypatch.setenv("BLOCKED_NETS", "none")
        importlib.reload(module)


def test_missing_blocked_nets_message_says_what_the_variable_is(monkeypatch):
    module = reload_with(monkeypatch, BLOCKED_NETS=None)
    try:
        with pytest.raises(RuntimeError) as e:
            importlib.reload(module)
    finally:
        monkeypatch.setenv("BLOCKED_NETS", "none")
        importlib.reload(module)
    message = str(e.value)
    assert "must never contact" in message and "none" in message and "model" not in message


@pytest.mark.parametrize("value", ["none", " NONE "])
def test_blocked_nets_none_means_no_site_networks(monkeypatch, caplog, value):
    module = reload_with(monkeypatch, BLOCKED_NETS=value)
    caplog.set_level(logging.INFO)
    importlib.reload(module)
    assert guard.BLOCKED_NETS == ()
    assert "0 site network(s) blocked" in caplog.text


def test_blocked_nets_are_given_to_the_guard_and_only_counted_in_the_log(monkeypatch, caplog):
    module = reload_with(monkeypatch, BLOCKED_NETS="172.60.0.0/16,10.99.0.0/16")
    caplog.set_level(logging.INFO)
    importlib.reload(module)
    assert guard.BLOCKED_NETS == (ip_network("172.60.0.0/16"), ip_network("10.99.0.0/16"))
    assert guard.is_public_url("http://site.example/", lambda host: ["172.60.0.127"]) is False
    assert guard.is_public_url("http://site.example/", lambda host: ["93.184.216.34"]) is True
    assert "2 site network(s) blocked" in caplog.text and "172.60" not in caplog.text


@pytest.mark.parametrize("value", ["garbage", "172.60.0.0/33", "none,10.0.0.0/8"])
def test_invalid_blocked_nets_stop_the_start(monkeypatch, value):
    module = reload_with(monkeypatch, BLOCKED_NETS=value)
    try:
        with pytest.raises((RuntimeError, ValueError), match="BLOCKED_NETS"):
            importlib.reload(module)
    finally:
        monkeypatch.setenv("BLOCKED_NETS", "none")
        importlib.reload(module)


@pytest.mark.parametrize("value", ["1", "", "0"])
def test_switch_that_stops_proxying_loopback_stops_the_start(monkeypatch, value):
    module = reload_with(monkeypatch, **{LOOPBACK_SWITCH: value})
    try:
        with pytest.raises(RuntimeError, match=LOOPBACK_SWITCH):
            importlib.reload(module)
    finally:
        monkeypatch.delenv(LOOPBACK_SWITCH)
        importlib.reload(module)


@pytest.mark.parametrize("value", ["ai.example", "ftp://ai.example", "https://", "http:///api", "//ai.example",
                                   "https://user@"])
def test_model_url_must_be_http_with_a_host(monkeypatch, value):
    module = reload_with(monkeypatch, LLM_BASE_URL=value)
    try:
        with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
            importlib.reload(module)
    finally:
        monkeypatch.setenv("LLM_BASE_URL", ENV["LLM_BASE_URL"])
        importlib.reload(module)


@pytest.mark.parametrize("value", ["http://open-webui:8080", "https://ai.example/base/"])
def test_model_url_with_http_and_host_is_accepted(monkeypatch, value):
    module = reload_with(monkeypatch, LLM_BASE_URL=value)
    assert importlib.reload(module).MODEL is not None
