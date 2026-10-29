"""Tests for link extraction, link safety and the trash actions (fake IMAP server)."""

from email.message import EmailMessage

import pytest

import app as unsub


def _mail(html: str, subject: str = "Newsletter") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "news@example.org"
    msg.set_content("plain")
    msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def test_finds_anchor_and_plain_links_without_duplicates():
    html = (
        '<a href="https://ex.org/u?id=1">Abmelden</a> <a href="https://ex.org/home">Home</a>'
        " https://ex.org/unsubscribe/2 https://ex.org/unsubscribe/2"
    )
    assert unsub.find_unsubscribe_links(html) == ["https://ex.org/u?id=1", "https://ex.org/unsubscribe/2"]


@pytest.mark.parametrize("href", ["javascript:alert(1)", "data:text/html,<script>x</script>", "JavaScript:x"])
def test_dangerous_links_are_dropped(href):
    assert unsub.find_unsubscribe_links(f'<a href="{href}">Unsubscribe</a>') == []


def test_encoded_subject_is_decoded():
    msg = EmailMessage()
    msg["Subject"] = "Grüße vom Newsletter"
    parsed = unsub.email.message_from_bytes(msg.as_bytes())
    assert unsub.get_subject(parsed) == "Grüße vom Newsletter"


class FakeIMAP:
    def __init__(self, mails: dict[bytes, bytes]):
        self.mails = mails
        self.calls = []

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "search":
            return "OK", [b" ".join(self.mails)]
        if command == "fetch":
            return "OK", [(b"1 (BODY[] {n}", self.mails[args[0]]), b")"]
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("expunge", ()))

    def logout(self):
        pass


def test_collect_links_keys_by_uid():
    fake = FakeIMAP({b"41": _mail('<a href="https://ex.org/u">unsubscribe</a>'), b"42": _mail("<p>no links</p>")})
    result = unsub.collect_links(fake)
    assert list(result) == ["41"] and result["41"]["links"] == ["https://ex.org/u"]


def test_move_to_trash_uses_uids_and_ignores_garbage(monkeypatch):
    fake = FakeIMAP({})
    monkeypatch.setattr(unsub, "connect", lambda: fake)
    assert unsub.move_emails_to_trash(["7", "1:*", "8 OR ALL"]) == 1
    assert ("copy", ("7", "Trash")) in fake.calls
    assert ("store", ("7", "+FLAGS", "(\\Deleted)")) in fake.calls
    assert not any(c[0] in ("copy", "store") and c[1][0] != "7" for c in fake.calls)


def test_move_all_form_posts_every_uid(monkeypatch):
    moved = []
    monkeypatch.setattr(unsub, "move_emails_to_trash", lambda uids: moved.extend(uids) or len(uids))
    client = unsub.app.test_client()
    assert client.post("/move-all-to-trash", data={"email_id": ["41", "42"]}).status_code == 302
    assert moved == ["41", "42"]


def test_result_page_renders_hidden_ids_for_move_all(monkeypatch):
    monkeypatch.setattr(unsub, "connect", lambda: FakeIMAP({b"41": _mail('<a href="https://ex.org/u">Abmelden</a>')}))
    html = unsub.app.test_client().get("/result").get_data(as_text=True)
    assert html.count('name="email_id" value="41"') == 2   # single form + move-all form
    assert 'rel="noopener noreferrer"' in html
