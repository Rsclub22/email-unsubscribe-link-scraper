"""Scheduler run: find cases, hand them to the agent, move successes, clean up (fake IMAP, fake agent)."""

import json
import time
from email.message import EmailMessage

import pytest

from cases import CaseStore
from worker import AgentUnavailable, Worker


def mail_bytes(mid, link="https://shop.example/unsubscribe/1", to="philipp@wagnersnetz.de", links=None,
               sender="Jens <jens@wagnersnetz.de>"):
    m = EmailMessage()
    m["Message-ID"] = mid
    m["Subject"] = "Fwd: News " + mid
    m["From"] = sender
    urls = links if links is not None else ([link] if link else [])
    m.set_content("Von: news@shop.example\nAn: " + to + "\n\n" + "\n".join(urls))
    return m.as_bytes()


class FakeIMAP:
    """Two folders; uid commands as far as the worker uses them."""

    def __init__(self, inbox):
        self.folders = {"INBOX": dict(inbox)}
        self.selected = "INBOX"
        self.deleted = set()
        self.fail_copy = False
        self.logged_out = False

    def select(self, folder):
        if folder not in self.folders:
            return "NO", [b"no such folder"]
        self.selected = folder
        return "OK", [b"1"]

    def create(self, folder):
        if folder in self.folders:
            return "NO", [b"exists"]
        self.folders[folder] = {}
        return "OK", [b"created"]

    def uid(self, command, *args):
        box = self.folders[self.selected]
        if command == "search":
            if len(args) >= 4 and args[1] == "HEADER":
                wanted = args[3].strip('"')
                hits = [u for u, raw in box.items() if ("Message-ID: " + wanted).encode() in raw]
                return "OK", [b" ".join(hits)]
            return "OK", [b" ".join(box)]
        if command == "fetch":
            return "OK", [(b"1 (BODY[] {n}", box[args[0]]), b")"]
        if command == "copy":
            if self.fail_copy or args[0] not in box:
                return "NO", [b"copy failed"]
            self.folders[args[1]][b"9" + args[0]] = box[args[0]]
            return "OK", [b""]
        if command == "store":
            self.deleted.add((self.selected, args[0]))
            return "OK", [b""]
        raise AssertionError(command)

    def expunge(self):
        for folder, uid in list(self.deleted):
            self.folders[folder].pop(uid, None)
        self.deleted.clear()
        return "OK", [b""]

    def logout(self):
        self.logged_out = True


def parse(message):
    import app as unsub
    text = unsub.get_text_from_email(message)
    return unsub.get_subject(message), unsub.find_unsubscribe_links(text), text


class Agent:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url, address):
        self.calls.append((url, address))
        r = self.results.pop(0) if self.results else {"result": "unclear", "reason": "no more results"}
        if isinstance(r, Exception):
            raise r
        return {"evidence": "", "evidence_url": "", "steps": 1, "stage": 2, **r}


OK = {"result": "unsubscribed", "reason": "page confirms", "evidence": "abgemeldet", "evidence_url": "https://shop.example/done"}


@pytest.fixture
def store(tmp_path):
    return CaseStore(str(tmp_path / "cases.db"))


def worker(store, imap, agent, **kw):
    kw.setdefault("allowed_senders", ("jens@wagnersnetz.de",))
    return Worker(store, lambda: imap, parse, agent, allowed_domains=("wagnersnetz.de",), **kw)


def test_success_is_moved_to_the_done_folder(store):
    imap = FakeIMAP({b"7": mail_bytes("<a@x>")})
    agent = Agent(OK)
    summary = worker(store, imap, agent).run_once()
    assert agent.calls == [("https://shop.example/unsubscribe/1", "philipp@wagnersnetz.de")]
    assert (summary["new"], summary["handled"], summary["unsubscribed"], summary["moved"], summary["error"]) == (1, 1, 1, 1, "")
    assert imap.folders["INBOX"] == {} and len(imap.folders["Erledigt"]) == 1 and imap.logged_out
    c = store.get("<a@x>")
    assert (c["state"], c["moved"], c["evidence"]) == ("unsubscribed", 1, "abgemeldet")
    assert json.loads(store.meta("last_run"))["moved"] == 1 and store.meta("last_run_at")


def test_failure_stays_in_the_inbox_and_is_not_tried_again(store):
    imap = FakeIMAP({b"7": mail_bytes("<a@x>")})
    agent = Agent({"result": "not_possible", "reason": "captcha"})
    w = worker(store, imap, agent)
    w.run_once()
    w.run_once()
    assert len(agent.calls) == 1 and b"7" in imap.folders["INBOX"]
    assert (store.get("<a@x>")["state"], store.get("<a@x>")["reason"]) == ("not_possible", "captcha")


def test_second_link_is_tried_but_not_a_third_and_not_after_login(store):
    links = ["https://shop.example/unsubscribe/1", "https://shop.example/unsubscribe/2", "https://shop.example/unsubscribe/3"]
    imap = FakeIMAP({b"1": mail_bytes("<two@x>", links=links), b"2": mail_bytes("<login@x>", links=links)})
    agent = Agent({"result": "unclear", "reason": "x"}, OK, {"result": "not_possible", "reason": "login"})
    worker(store, imap, agent).run_once()
    assert [u for u, _ in agent.calls] == [links[0], links[1], links[0]]
    assert store.get("<two@x>")["state"] == "unsubscribed"
    assert (store.get("<login@x>")["state"], store.get("<login@x>")["reason"]) == ("not_possible", "login")


def test_agent_error_does_not_burn_the_attempt(store):
    imap = FakeIMAP({b"1": mail_bytes("<a@x>"), b"2": mail_bytes("<b@x>")})
    agent = Agent(AgentUnavailable("agent not reachable"))
    w = worker(store, imap, agent)
    summary = w.run_once()
    assert summary["error"] == "agent not reachable" and summary["handled"] == 0 and len(agent.calls) == 1
    assert [(c["state"], c["attempts"]) for c in store.all()] == [("new", 0), ("new", 0)]
    agent.results = [OK, OK]
    assert w.run_once()["unsubscribed"] == 2


def test_model_unavailable_result_counts_like_an_unavailable_agent(store):
    imap = FakeIMAP({b"1": mail_bytes("<a@x>")})
    summary = worker(store, imap, Agent({"result": "error", "reason": "llm_unavailable"})).run_once()
    assert summary["error"] == "llm_unavailable" and store.get("<a@x>")["attempts"] == 0


def test_without_agent_cases_are_only_recorded(store):
    imap = FakeIMAP({b"1": mail_bytes("<a@x>"), b"2": mail_bytes("<nolink@x>", link="")})
    summary = worker(store, imap, None).run_once()
    assert (summary["new"], summary["handled"], summary["without_link"]) == (1, 0, 1)
    assert store.get("<a@x>")["state"] == "new" and store.get("<nolink@x>") is None


def test_paused_run_does_nothing(store):
    store.set_meta("paused", "1")
    imap = FakeIMAP({b"1": mail_bytes("<a@x>")})
    agent = Agent(OK)
    summary = worker(store, imap, agent).run_once()
    assert summary["error"] == "paused" and agent.calls == [] and store.all() == []


def test_at_most_max_cases_per_run(store):
    imap = FakeIMAP({str(i).encode(): mail_bytes(f"<{i}@x>") for i in range(1, 5)})
    agent = Agent(OK, OK, OK, OK)
    assert worker(store, imap, agent, max_cases=3).run_once()["handled"] == 3
    assert len(agent.calls) == 3


def test_address_of_a_foreign_domain_is_not_handed_over(store):
    imap = FakeIMAP({b"1": mail_bytes("<a@x>", to="victim@elsewhere.example")})
    agent = Agent(OK)
    worker(store, imap, agent).run_once()
    assert agent.calls == [("https://shop.example/unsubscribe/1", "jens@wagnersnetz.de")]   # the forwarder


def test_move_failure_keeps_case(store):
    imap = FakeIMAP({b"7": mail_bytes("<a@x>")})
    imap.fail_copy = True
    w = worker(store, imap, Agent(OK))
    summary = w.run_once()
    assert summary["moved"] == 0 and b"7" in imap.folders["INBOX"]
    assert (store.get("<a@x>")["state"], store.get("<a@x>")["moved"]) == ("unsubscribed", 0)
    imap.fail_copy = False
    assert w.run_once()["moved"] == 1 and imap.folders["INBOX"] == {}


def test_cleanup_deletes_old_successes_from_the_done_folder(store):
    imap = FakeIMAP({b"7": mail_bytes("<old@x>"), b"8": mail_bytes("<young@x>")})
    now = [time.time()]   # store stamps real time; a fixed start date would make the test date-dependent
    w = worker(store, imap, Agent(OK, OK), clock=lambda: now[0], retention_days=30)
    w.run_once()
    assert len(imap.folders["Erledigt"]) == 2
    young_time = store.get("<young@x>")["updated_at"]
    store._run("UPDATE cases SET updated_at = ? WHERE key = '<old@x>'", (young_time - 31 * 86400,))
    now[0] = young_time + 60
    summary = w.run_once()
    assert summary["cleaned"] == 1
    assert store.get("<old@x>") is None and store.get("<young@x>") is not None
    assert len(imap.folders["Erledigt"]) == 1 and imap.selected == "INBOX"


def test_run_survives_a_broken_mail(store):
    imap = FakeIMAP({b"1": b"\xff\xfe not a mail", b"2": mail_bytes("<b@x>")})
    assert worker(store, imap, Agent(OK)).run_once()["unsubscribed"] == 1


def test_mail_from_unknown_sender_is_ignored(store):
    imap = FakeIMAP({b"1": mail_bytes("<a@x>", sender="Jens <JENS@wagnersnetz.de>"),
                     b"2": mail_bytes("<evil@x>", sender="Mallory <mallory@evil.example>")})
    agent = Agent(OK)
    summary = worker(store, imap, agent, allowed_senders=("jens@wagnersnetz.de",)).run_once()
    assert len(agent.calls) == 1 and summary["ignored"] == 1
    assert [c["key"] for c in store.all()] == ["<a@x>"]
    assert store.get("<evil@x>") is None and b"2" in imap.folders["INBOX"]


def test_no_allowed_senders_with_agent_means_nobody(store):
    imap = FakeIMAP({b"1": mail_bytes("<a@x>")})
    agent = Agent(OK)
    summary = worker(store, imap, agent, allowed_senders=()).run_once()
    assert agent.calls == [] and store.all() == [] and summary["ignored"] == 1


def test_no_allowed_senders_without_agent_lists_everything(store):
    imap = FakeIMAP({b"1": mail_bytes("<a@x>")})
    summary = worker(store, imap, None, allowed_senders=()).run_once()
    assert store.get("<a@x>")["state"] == "new" and summary["ignored"] == 0


def test_spoofed_display_name_does_not_help(store):
    imap = FakeIMAP({b"1": mail_bytes("<a@x>", sender='"jens@wagnersnetz.de" <mallory@evil.example>')})
    agent = Agent(OK)
    summary = worker(store, imap, agent, allowed_senders=("jens@wagnersnetz.de",)).run_once()
    assert agent.calls == [] and store.all() == [] and summary["ignored"] == 1
