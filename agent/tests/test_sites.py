"""The browser loop against imitated unsubscribe pages. Needs Playwright: run with tests/run-in-docker.sh."""

import logging
import os
import tempfile
import time
from urllib.parse import quote

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

import guard                                   # noqa: E402
from llm import LLMUnavailable                 # noqa: E402
from runner import BROWSER_ARGS, run_case      # noqa: E402
from tests import sites                        # noqa: E402


class Script:
    """Stands in for the model: answers with a fixed list of replies and records what it was shown."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def ask(self, system, user):
        self.prompts.append(user)
        reply = self.replies.pop(0) if self.replies else '{"action":"done","result":"unclear","reason":"script empty"}'
        if isinstance(reply, Exception):
            raise reply
        return reply


def element(prompt: str, label: str) -> int:
    """Number of the first listed element whose line contains the label."""
    for line in prompt.splitlines():
        if line.startswith("[") and label in line:
            return int(line[1:line.index("]")])
    raise AssertionError(f"no element with {label!r} in:\n{prompt}")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(args=BROWSER_ARGS)
        yield b
        b.close()


@pytest.fixture
def site():
    server, base = sites.start()
    yield base
    server.shutdown()


@pytest.fixture
def other():
    server, base, log = sites.start_other()
    yield base, log
    server.shutdown()


def local_ok(base):
    """Guard for the tests: the test server on 127.0.0.1 counts as public, everything else goes to the real guard."""
    return lambda url: url.startswith(base) or guard.is_public_url(url)


def local_addresses(base):
    """The matching check for the egress proxy: the test server is reached at 127.0.0.1, all else as in production."""
    return lambda url: ["127.0.0.1"] if url.startswith(base + "/") else guard.public_addresses(url)


def run(browser, site, path, llm, address="philipp@wagnersnetz.de", **kw):
    return run_case(browser, site + path, address, llm, url_ok=local_ok(site), addresses_for=local_addresses(site),
                    nav_timeout=10, **kw)


def test_one_click_page_needs_no_model(browser, site):
    llm = Script()
    r = run(browser, site, "/oneclick", llm)
    assert (r["result"], r["stage"], r["steps"]) == ("unsubscribed", 1, 0)
    assert "You have been unsubscribed" in r["evidence"] and r["evidence_url"].endswith("/oneclick")
    assert llm.prompts == []


def test_confirm_button(browser, site):
    class Confirm(Script):
        def ask(self, system, user):
            self.prompts.append(user)
            if "erfolgreich abgemeldet" in user:
                return '{"action":"done","result":"unsubscribed","reason":"page confirms"}'
            return '{"action":"click","element":%d}' % element(user, "Abmelden bestätigen")

    r = run(browser, site, "/confirm", Confirm())
    assert (r["result"], r["stage"]) == ("unsubscribed", 2)
    assert ("POST", "/done", {}) in sites.Handler.log
    assert "erfolgreich abgemeldet" in r["evidence"]


def test_reason_select_checkbox_and_submit(browser, site):
    class Reason(Script):
        def ask(self, system, user):
            self.prompts.append(user)
            n = len(self.prompts)
            if n == 1:
                return '{"action":"select","element":%d,"option":"Zu viele Mails"}' % element(user, "select")
            if n == 2:
                return '{"action":"check","element":%d,"on":true}' % element(user, "checkbox")
            if n == 3:
                return '{"action":"click","element":%d}' % element(user, "Absenden")
            return '{"action":"done","result":"unsubscribed","reason":"confirmed"}'

    r = run(browser, site, "/reason", Reason())
    assert r["result"] == "unsubscribed"
    assert ("POST", "/done", {"why": ["Zu viele Mails"], "all": ["on"]}) in sites.Handler.log


def test_address_is_inserted_by_code_not_by_the_model(browser, site):
    class Address(Script):
        def ask(self, system, user):
            self.prompts.append(user)
            n = len(self.prompts)
            if n == 1:
                return '{"action":"address","element":%d,"value":"attacker@evil.example"}' % element(user, "email")
            if n == 2:
                return '{"action":"click","element":%d}' % element(user, "Abmelden")
            return '{"action":"done","result":"unsubscribed","reason":"confirmed"}'

    r = run(browser, site, "/address", Address(), address="anne@wagnersnetz.de")
    assert r["result"] == "unsubscribed"
    assert ("POST", "/done-address", {"email": ["anne@wagnersnetz.de"]}) in sites.Handler.log


def test_login_page_ends_without_asking_the_model(browser, site):
    llm = Script()
    r = run(browser, site, "/login", llm)
    assert (r["result"], r["reason"]) == ("not_possible", "login") and llm.prompts == []


def test_captcha_ends_without_asking_the_model(browser, site):
    llm = Script()
    r = run(browser, site, "/captcha", llm)
    assert (r["result"], r["reason"]) == ("not_possible", "captcha") and llm.prompts == []


def test_injected_instructions_reach_the_model_only_as_page_text(browser, site):
    class Obedient(Script):
        """A model that falls for the injection: it clicks 'Buy now' and then claims success."""

        def ask(self, system, user):
            self.prompts.append(user)
            if len(self.prompts) == 1:
                return '{"action":"click","element":%d}' % element(user, "Buy now")
            return '{"action":"done","result":"unsubscribed","reason":"done as told"}'

    llm = Obedient()
    r = run(browser, site, "/inject", llm)
    first = llm.prompts[0]
    assert first.index("<page_text>") < first.index("ignore all previous rules") < first.index("</page_text>")
    # the damage a fooled model can do is a click on a public page; the evidence shows what really happened
    assert r["evidence_url"].endswith("/bought") and "purchase" in r["evidence"]
    assert not any(method == "POST" for method, _, _ in sites.Handler.log)


def test_endless_pages_end_at_the_step_budget(browser, site):
    class Clicker(Script):
        def ask(self, system, user):
            self.prompts.append(user)
            return '{"action":"click","element":%d}' % element(user, "Bestätigen")

    r = run(browser, site, "/loop", Clicker(), max_steps=4)
    assert (r["result"], r["reason"], r["steps"]) == ("not_possible", "budget", 4)


def test_time_budget_ends_the_case(browser, site):
    ticks = iter(range(0, 10000, 200))
    r = run(browser, site, "/loop", Script('{"action":"click","element":1}'), case_timeout=300, clock=lambda: next(ticks))
    assert (r["result"], r["reason"]) == ("not_possible", "budget")


def test_three_bad_replies_end_unclear(browser, site):
    llm = Script("I think I should click.", '{"action":"type","element":1,"text":"x"}', '{"action":"click","element":99}')
    r = run(browser, site, "/confirm", llm)
    assert (r["result"], r["steps"]) == ("unclear", 3) and "invalid replies" in r["reason"]
    assert not any(method == "POST" for method, _, _ in sites.Handler.log)


def test_a_good_reply_resets_the_bad_reply_count(browser, site):
    class Mixed(Script):
        def ask(self, system, user):
            self.prompts.append(user)
            n = len(self.prompts)
            if n in (1, 2):
                return "prose"
            if n == 3:
                return '{"action":"click","element":%d}' % element(user, "Abmelden bestätigen")
            return '{"action":"done","result":"unsubscribed","reason":"ok"}'

    assert run(browser, site, "/confirm", Mixed())["result"] == "unsubscribed"


def test_start_address_must_be_public(browser, site):
    r = run_case(browser, "http://192.168.8.111/unsubscribe", "", Script(), url_ok=guard.is_public_url)
    assert (r["result"], r["reason"]) == ("not_possible", "blocked_address")


def test_redirect_to_private_address_is_blocked(browser, site):
    r = run(browser, site, "/to-private", Script())
    assert (r["result"], r["reason"]) == ("not_possible", "blocked_address")


def test_embedded_private_content_is_blocked_but_the_page_still_works(browser, site):
    class Confirm(Script):
        def ask(self, system, user):
            self.prompts.append(user)
            if "erfolgreich abgemeldet" in user:
                return '{"action":"done","result":"unsubscribed","reason":"ok"}'
            return '{"action":"click","element":%d}' % element(user, "Abmelden bestätigen")

    assert run(browser, site, "/embeds-private", Confirm())["result"] == "unsubscribed"


def _files_with(content: bytes) -> set[str]:
    """Every file below the temp directory (where Playwright keeps downloads) whose content is exactly content."""
    found = set()
    for root, _, files in os.walk(tempfile.gettempdir()):
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) == len(content) and open(path, "rb").read() == content:
                    found.add(path)
            except OSError:
                pass
    return found


def test_download_is_not_kept(browser, site, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="runner")
    before = _files_with(b"data")

    class Download(Script):
        def ask(self, system, user):
            self.prompts.append(user)
            if len(self.prompts) == 1:
                return '{"action":"click","element":%d}' % element(user, "Abmelden bestätigen")
            return '{"action":"done","result":"unclear","reason":"nothing happened"}'

    r = run(browser, site, "/download", Download())
    assert r["result"] == "unclear"
    assert ("GET", "/file.bin", {}) in sites.Handler.log            # the download really was attempted
    assert "download refused" in caplog.text                         # the browser offered it and it was refused
    assert _files_with(b"data") - before == set()                    # and no file with its content appeared


def test_model_unavailable_is_an_error_not_a_result(browser, site):
    r = run(browser, site, "/confirm", Script(LLMUnavailable("model endpoint answered 502")))
    assert (r["result"], r["reason"]) == ("error", "llm_unavailable")


def test_no_address_means_the_address_action_is_refused(browser, site):
    llm = Script('{"action":"address","element":1}', '{"action":"done","result":"not_possible","reason":"needs address"}')
    r = run(browser, site, "/address", llm, address="")
    assert r["result"] == "not_possible" and "NOT available" in llm.prompts[0]
    assert not any(method == "POST" for method, _, _ in sites.Handler.log)


def test_page_that_does_not_load_is_a_page_error(browser, site):
    r = run_case(browser, "http://127.0.0.1:9/gone", "", Script(), url_ok=lambda url: True,
                 addresses_for=lambda url: ["127.0.0.1"], nav_timeout=5)
    assert (r["result"], r["reason"]) == ("not_possible", "page_error")


def test_redirect_to_loopback_never_leaves_the_proxy(browser, site, other):
    base2, log2 = other
    r = run(browser, site, "/redirect?to=" + quote(base2 + "/admin"), Script())
    assert log2 == []
    assert (r["result"], r["reason"]) == ("not_possible", "blocked_address")


@pytest.mark.parametrize("page", ["/img?src=", "/fetch?to="])
@pytest.mark.parametrize("via_redirect", [False, True])
def test_subresources_to_loopback_never_reach_it(browser, site, other, page, via_redirect):
    base2, log2 = other
    target = base2 + "/pixel.gif"
    if via_redirect:                 # the route gate sees only the first hop; the proxy must stop the second
        target = site + "/redirect?to=" + quote(target)
    run(browser, site, page + quote(target), Script())
    time.sleep(0.5)
    assert log2 == []
    if via_redirect:
        assert ("GET", "/redirect", {}) in sites.Handler.log      # the first hop did go out


def test_endless_script_ends_at_the_time_budget(browser, site):
    start = time.monotonic()
    r = run(browser, site, "/busy", Script(), case_timeout=5)
    assert time.monotonic() - start < 15
    assert (r["result"], r["reason"]) == ("not_possible", "budget")


def test_slow_model_ends_at_the_time_budget_and_gets_a_capped_timeout(browser, site):
    class Slow(Script):
        timeouts = []

        def ask(self, system, user, timeout=None):
            self.prompts.append(user)
            self.timeouts.append(timeout)
            time.sleep(4)
            return '{"action":"click","element":%d}' % element(user, "Abmelden bestätigen")

    llm = Slow()
    start = time.monotonic()
    r = run(browser, site, "/confirm", llm, case_timeout=3)
    assert time.monotonic() - start < 10
    assert (r["result"], r["reason"]) == ("not_possible", "budget")
    assert llm.timeouts and all(t is not None and 0 < t <= 3 for t in llm.timeouts)
    assert not any(method == "POST" for method, _, _ in sites.Handler.log)


def test_webrtc_is_not_available_to_pages(browser, site):
    llm = Script()
    run(browser, site, "/rtc", llm)
    assert "rtc:undefined,undefined" in llm.prompts[0]


def test_blocked_image_on_a_page_that_never_loads_is_a_page_error(browser, site):
    start = time.monotonic()
    r = run_case(browser, site + "/hang-img", "", Script(), url_ok=local_ok(site), addresses_for=local_addresses(site),
                 nav_timeout=3)
    assert (r["result"], r["reason"]) == ("not_possible", "page_error")
    assert time.monotonic() - start < 8


@pytest.mark.parametrize("path", ["/many-links-password", "/frame-password"])
def test_password_field_anywhere_ends_without_the_model(browser, site, path):
    llm = Script()
    r = run(browser, site, path, llm)
    assert (r["result"], r["reason"]) == ("not_possible", "login") and llm.prompts == []
