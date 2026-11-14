"""Case store: one case per forwarded mail, keyed by Message-ID."""

from email.message import EmailMessage

import pytest

from cases import CaseStore, case_key


@pytest.fixture
def store(tmp_path):
    return CaseStore(str(tmp_path / "cases.db"))


def _msg(mid="<a@x>", subject="News"):
    m = EmailMessage()
    if mid:
        m["Message-ID"] = mid
    m["Subject"] = subject
    m["Date"] = "Sat, 14 Nov 2026 10:00:00 +0100"
    m["From"] = "philipp@wagnersnetz.de"
    return m


def test_key_is_message_id_or_stable_hash():
    assert case_key(_msg("<a@x>")) == "<a@x>"
    k = case_key(_msg(""))
    assert k.startswith("sha256:") and k == case_key(_msg("")) and k != case_key(_msg("", "Other"))


def test_add_is_idempotent_and_keeps_links(store):
    assert store.add("<a@x>", "News", ["https://e.org/u1", "https://e.org/u2"], "p@wagnersnetz.de") is True
    assert store.add("<a@x>", "changed", [], "") is False
    c = store.get("<a@x>")
    assert (c["subject"], c["links"], c["address"], c["state"], c["attempts"], c["moved"]) == (
        "News", ["https://e.org/u1", "https://e.org/u2"], "p@wagnersnetz.de", "new", 0, 0)


def test_pending_only_untried_new_cases_oldest_first(store):
    for k in ("<1>", "<2>", "<3>"):
        store.add(k, k, ["https://e.org"], "")
    store.claim("<1>")
    store.finish("<1>", "unclear", "x")
    assert [c["key"] for c in store.pending(10)] == ["<2>", "<3>"]
    assert [c["key"] for c in store.pending(1)] == ["<2>"]


def test_claim_counts_attempt_and_release_gives_it_back(store):
    store.add("<1>", "s", ["https://e.org"], "")
    store.claim("<1>")
    assert (store.get("<1>")["state"], store.get("<1>")["attempts"]) == ("running", 1)
    store.release("<1>")
    assert (store.get("<1>")["state"], store.get("<1>")["attempts"]) == ("new", 0)
    assert [c["key"] for c in store.pending(10)] == ["<1>"]


def test_finish_stores_reason_and_evidence(store):
    store.add("<1>", "s", ["https://e.org"], "")
    store.claim("<1>")
    store.finish("<1>", "unsubscribed", "page confirms", "You have been unsubscribed", "https://e.org/done")
    c = store.get("<1>")
    assert (c["state"], c["reason"], c["evidence"], c["evidence_url"]) == (
        "unsubscribed", "page confirms", "You have been unsubscribed", "https://e.org/done")
    with pytest.raises(ValueError):
        store.finish("<1>", "great", "")


def test_to_move_and_mark_moved(store):
    store.add("<1>", "s", ["https://e.org"], "")
    store.claim("<1>")
    store.finish("<1>", "unsubscribed")
    assert [c["key"] for c in store.to_move()] == ["<1>"]
    store.mark_moved("<1>")
    assert store.to_move() == []


def test_retry_makes_a_failed_case_pending_again(store):
    store.add("<1>", "s", ["https://e.org"], "")
    store.claim("<1>")
    store.finish("<1>", "not_possible", "login")
    store.retry("<1>")
    c = store.get("<1>")
    assert (c["state"], c["attempts"], c["reason"]) == ("new", 0, "")


def test_expired_only_moved_successes_older_than_days(store):
    for k in ("<old>", "<young>", "<failed>"):
        store.add(k, "s", ["https://e.org"], "")
        store.claim(k)
    store.finish("<old>", "unsubscribed")
    store.finish("<young>", "unsubscribed")
    store.finish("<failed>", "unclear")
    store.mark_moved("<old>")
    store.mark_moved("<young>")
    now = store.get("<old>")["updated_at"]
    assert [c["key"] for c in store.expired(30, now=now + 31 * 86400)] == ["<old>", "<young>"]
    assert store.expired(30, now=now + 29 * 86400) == []
    store.delete("<old>")
    assert store.get("<old>") is None


def test_recover_turns_running_into_unclear(store):
    store.add("<1>", "s", ["https://e.org"], "")
    store.claim("<1>")
    assert store.recover() == 1
    c = store.get("<1>")
    assert (c["state"], c["reason"], c["attempts"]) == ("unclear", "interrupted by restart", 1)


def test_meta_roundtrip(store):
    assert store.meta("paused", "0") == "0"
    store.set_meta("paused", "1")
    assert store.meta("paused") == "1"


def test_key_copes_with_a_non_ascii_message_id():
    import email
    raw = "Message-ID: <näh@x>\r\nSubject: s\r\n\r\nbody".encode("utf-8")
    message = email.message_from_bytes(raw)
    key = case_key(message)
    assert isinstance(key, str) and key
    assert case_key(message) == key
