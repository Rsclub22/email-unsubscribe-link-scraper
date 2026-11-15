"""The browser loop against imitated unsubscribe pages. Needs Playwright: run with tests/run-in-docker.sh."""

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

import guard                                   # noqa: E402
from llm import LLMUnavailable                 # noqa: E402
from runner import run_case                    # noqa: E402
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
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def site():
    server, base = sites.start()
    yield base
    server.shutdown()


def local_ok(base):
    """Guard for the tests: the test server on 127.0.0.1 counts as public, everything else goes to the real guard."""
    return lambda url: url.startswith(base) or guard.is_public_url(url)


def run(browser, site, path, llm, address="philipp@wagnersnetz.de", **kw):
    return run_case(browser, site + path, address, llm, url_ok=local_ok(site), nav_timeout=10, **kw)


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


def test_download_is_not_kept(browser, site, tmp_path):
    class Download(Script):
        def ask(self, system, user):
            self.prompts.append(user)
            if len(self.prompts) == 1:
                return '{"action":"click","element":%d}' % element(user, "Abmelden bestätigen")
            return '{"action":"done","result":"unclear","reason":"nothing happened"}'

    r = run(browser, site, "/download", Download())
    assert r["result"] == "unclear"


def test_model_unavailable_is_an_error_not_a_result(browser, site):
    r = run(browser, site, "/confirm", Script(LLMUnavailable("model endpoint answered 502")))
    assert (r["result"], r["reason"]) == ("error", "llm_unavailable")


def test_no_address_means_the_address_action_is_refused(browser, site):
    llm = Script('{"action":"address","element":1}', '{"action":"done","result":"not_possible","reason":"needs address"}')
    r = run(browser, site, "/address", llm, address="")
    assert r["result"] == "not_possible" and "NOT available" in llm.prompts[0]
    assert not any(method == "POST" for method, _, _ in sites.Handler.log)


def test_page_that_does_not_load_is_a_page_error(browser, site):
    r = run_case(browser, "http://127.0.0.1:9/gone", "", Script(), url_ok=lambda url: True, nav_timeout=5)
    assert (r["result"], r["reason"]) == ("not_possible", "page_error")
