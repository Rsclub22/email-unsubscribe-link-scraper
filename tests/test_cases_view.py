"""Case view and its actions (the store is real, the mailbox is faked)."""

import importlib
import json

import pytest


@pytest.fixture
def unsub(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_SCHEDULER", "0")
    monkeypatch.setenv("AGENT_URL", "http://agent.test:8080")
    monkeypatch.setenv("AGENT_ALLOWED_DOMAINS", "wagnersnetz.de, gmail.com")
    monkeypatch.setenv("AGENT_ALLOWED_SENDERS", "Jens@wagnersnetz.de, philipp@wagnersnetz.de")
    for name in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "TOKEN_KEY"):
        monkeypatch.delenv(name, raising=False)
    import app
    module = importlib.reload(app)
    yield module
    monkeypatch.delenv("DATA_DIR")
    monkeypatch.delenv("AGENT_URL")
    importlib.reload(app)   # the other test modules expect the app without a store


def test_settings_from_environment(unsub):
    assert unsub.STORE is not None and unsub.WORKER is not None
    assert unsub.WORKER.allowed_domains == ("wagnersnetz.de", "gmail.com")
    assert unsub.WORKER.allowed_senders == ("jens@wagnersnetz.de", "philipp@wagnersnetz.de")
    assert unsub.WORKER.agent is unsub.call_agent


def test_result_shows_cases_with_state_reason_evidence_and_links(unsub, monkeypatch):
    s = unsub.STORE
    s.add("<ok@x>", "Fwd: Shop <b>News</b>", ["https://shop.example/u/1"], "p@wagnersnetz.de")
    s.claim("<ok@x>")
    s.finish("<ok@x>", "unsubscribed", "page confirms", "Sie wurden abgemeldet", "https://shop.example/done")
    s.add("<no@x>", "Fwd: Bank", ["https://bank.example/u"], "")
    s.claim("<no@x>")
    s.finish("<no@x>", "not_possible", "login")
    s.set_meta("last_run", json.dumps({"new": 2, "handled": 2, "unsubscribed": 1, "moved": 1, "cleaned": 0,
                                       "without_link": 3, "ignored": 2, "error": ""}))
    s.set_meta("last_run_at", "1800000000")
    monkeypatch.setattr(unsub, "scan_inbox", lambda: {"<no@x>": b"42"})
    html = unsub.app.test_client().get("/result").get_data(as_text=True)
    assert "Fwd: Shop &lt;b&gt;News&lt;/b&gt;" in html and "<b>News</b>" not in html
    assert "unsubscribed" in html and "Sie wurden abgemeldet" in html and "https://shop.example/done" in html
    assert "not possible" in html and "login" in html and 'href="https://bank.example/u"' in html
    assert 'name="email_id" value="42"' in html              # trash button only for mails still in the inbox
    assert html.count('action="/retry"') == 1                 # only the failed case can be retried
    assert "3 mail(s) without unsubscribe link" in html
    assert "2 mail(s) from senders that are not allowed" in html


def test_result_still_lists_cases_when_the_inbox_scan_fails(unsub, monkeypatch):
    from worker import ScanFailed
    s = unsub.STORE
    s.add("<no@x>", "Fwd: Bank", ["https://bank.example/u"], "")
    s.claim("<no@x>")
    s.finish("<no@x>", "not_possible", "login")

    def failing():
        raise ScanFailed("inbox scan failed")
    monkeypatch.setattr(unsub, "scan_inbox", failing)
    response = unsub.app.test_client().get("/result")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Fwd: Bank" in html and "The inbox could not be read" in html
    assert 'name="email_id"' not in html and 'action="/retry"' not in html   # nothing is known about the mails


def test_max_deferrals_default_and_from_environment(unsub, monkeypatch):
    import app
    assert unsub.WORKER.max_deferrals == 12
    monkeypatch.setenv("AGENT_MAX_DEFERRALS", "3")
    assert importlib.reload(app).WORKER.max_deferrals == 3


def test_result_shows_cases_waiting_for_the_model(unsub, monkeypatch):
    unsub.STORE.set_meta("last_run", json.dumps({"new": 0, "deferred": 2, "error": ""}))
    monkeypatch.setattr(unsub, "scan_inbox", lambda: {})
    html = unsub.app.test_client().get("/result").get_data(as_text=True)
    assert "2 case(s) waiting for the model." in html


def test_retry_only_for_a_case_with_its_mail_in_the_inbox(unsub, monkeypatch):
    s = unsub.STORE
    for key in ("<here@x>", "<gone@x>"):
        s.add(key, key, ["https://bank.example/u"], "")
        s.claim(key)
        s.finish(key, "not_possible", "login")
    monkeypatch.setattr(unsub, "scan_inbox", lambda: {"<here@x>": b"42"})
    html = unsub.app.test_client().get("/result").get_data(as_text=True)
    assert html.count('action="/retry"') == 1 and 'name="key" value="&lt;here@x&gt;"' in html
    assert 'name="key" value="&lt;gone@x&gt;"' not in html


def test_unmoved_success_shows_not_moved_yet(unsub, monkeypatch):
    s = unsub.STORE
    for key in ("<unmoved@x>", "<moved@x>"):
        s.add(key, key, ["https://shop.example/u"], "")
        s.claim(key)
        s.finish(key, "unsubscribed", "page confirms")
    s.mark_moved("<moved@x>")
    monkeypatch.setattr(unsub, "scan_inbox", lambda: {"<unmoved@x>": b"7"})
    html = unsub.app.test_client().get("/result").get_data(as_text=True)
    assert html.count("not moved yet") == 1
    row = html[html.index("&lt;unmoved@x&gt;"):]
    assert row.index("not moved yet") < row.index("</tr>")


def test_retry_resets_the_case(unsub):
    s = unsub.STORE
    s.add("<no@x>", "s", ["https://bank.example/u"], "")
    s.claim("<no@x>")
    s.finish("<no@x>", "unclear", "x")
    r = unsub.app.test_client().post("/retry", data={"key": "<no@x>"})
    assert r.status_code == 302 and s.get("<no@x>")["state"] == "new" and s.get("<no@x>")["attempts"] == 0


def test_pause_and_resume(unsub):
    c = unsub.app.test_client()
    c.post("/pause", data={"paused": "1"})
    assert unsub.STORE.meta("paused") == "1"
    c.post("/pause", data={"paused": "0"})
    assert unsub.STORE.meta("paused") == "0"


def test_run_now_starts_a_background_run_and_returns_at_once(unsub, monkeypatch):
    calls, threads = [], []
    real_start_run = unsub.start_run

    def keep_thread():
        threads.append(real_start_run())
        return threads[-1]

    monkeypatch.setattr(unsub, "start_run", keep_thread)
    monkeypatch.setattr(unsub.WORKER, "run_once", lambda: calls.append(1) or {"error": ""})
    assert unsub.app.test_client().post("/run-now").status_code == 302
    threads[0].join(timeout=5)
    assert calls == [1]

    def boom():
        raise RuntimeError("secret mail content")

    monkeypatch.setattr(unsub.WORKER, "run_once", boom)
    assert unsub.app.test_client().post("/run-now").status_code == 302
    threads[1].join(timeout=5)
    assert json.loads(unsub.STORE.meta("last_run"))["error"] == "RuntimeError"


def test_call_agent_maps_failures_to_unavailable(unsub, monkeypatch):
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(unsub.httpx, "post", boom)
    with pytest.raises(unsub.AgentUnavailable):
        unsub.call_agent("https://shop.example/u", "p@wagnersnetz.de")
    monkeypatch.setattr(unsub.httpx, "post", lambda *a, **kw: httpx.Response(500, text="x", request=httpx.Request("POST", a[0])))
    with pytest.raises(unsub.AgentUnavailable):
        unsub.call_agent("https://shop.example/u", "p@wagnersnetz.de")


def test_call_agent_sends_only_url_and_address(unsub, monkeypatch):
    import httpx
    seen = {}

    def post(url, json=None, timeout=None):
        seen.update(url=url, json=json, timeout=timeout)
        return httpx.Response(200, json={"result": "unclear", "reason": "x"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(unsub.httpx, "post", post)
    assert unsub.call_agent("https://shop.example/u", "p@wagnersnetz.de")["result"] == "unclear"
    assert seen["url"] == "http://agent.test:8080/run"
    assert seen["json"] == {"url": "https://shop.example/u", "address": "p@wagnersnetz.de"}
    assert seen["timeout"] > 600   # two links, 5 minutes each


def test_without_data_dir_the_old_list_is_shown():
    import app
    assert app.STORE is None and app.WORKER is None
