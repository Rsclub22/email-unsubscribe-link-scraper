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


def test_escaped_ampersand_in_an_href_does_not_give_a_second_mangled_link():
    html = ('<a href="https://ex.org/unsubscribe?u=1&amp;t=abc">Abmelden</a>'
            ' <a href="https://ex.org/unsubscribe-all?u=1">Von allen abmelden</a>')
    assert unsub.find_unsubscribe_links(html) == ["https://ex.org/unsubscribe?u=1&t=abc",
                                                  "https://ex.org/unsubscribe-all?u=1"]


def test_plain_text_link_keeps_its_place_behind_an_escaped_duplicate():
    html = ('<a href="https://ex.org/unsubscribe?u=1&amp;t=abc">Abmelden</a>'
            ' oder hier: https://ex.org/unsubscribe/2.')
    assert unsub.find_unsubscribe_links(html) == ["https://ex.org/unsubscribe?u=1&t=abc",
                                                  "https://ex.org/unsubscribe/2"]


@pytest.mark.parametrize("tail", [".", ",", ";", ":", "!", "?", ")", "”", "’", "»", ").", "!”"])
def test_punctuation_after_a_plain_text_link_is_not_part_of_it(tail):
    text = "Abmelden (siehe https://ex.org/unsubscribe/2" + tail + " danach)"
    assert unsub.find_unsubscribe_links(text) == ["https://ex.org/unsubscribe/2"]


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


# --- link named "hier": the word that says "unsubscribe" stands next to the link, in the same sentence ---

LIEFERANDO = (
    '<table><tr><td>Wir schicken Dir diese E-Mail, weil Du mit der E-Mail-Adresse '
    '<a href="mailto:philipp@example.org">philipp@example.org</a> unseren Newsletter abonniert hast.<br/>'
    'Wenn Du keinen Newsletter von uns mehr bekommen möchtest, kannst Du ihn '
    '<a href="https://click.example/u/1"><u>hier</u></a> abbestellen.<br/><br/>'
    'Lieferando ist ein Angebot von Takeaway.com<br/>'
    'E-Mail <a href="https://click.example/view/2"><u>online</u></a> ansehen.</td></tr></table>'
)


def test_link_named_hier_counts_when_its_sentence_says_unsubscribe():
    assert unsub.find_unsubscribe_links(LIEFERANDO) == ["https://click.example/u/1"]


@pytest.mark.parametrize("html", [
    'To unsubscribe, click <a href="https://click.example/u/1">here</a>.',
    '<p>Du kannst dich <a href="https://click.example/u/1"><span>hier</span></a> <b>abmelden</b>.</p>',
    '<div>Newsletter <a href="https://click.example/u/1">hier</a>\n   abbestellen</div>',
])
def test_sentence_around_a_short_link_is_read_across_inline_tags(html):
    assert unsub.find_unsubscribe_links(html) == ["https://click.example/u/1"]


@pytest.mark.parametrize("html", [
    # another sentence of the same paragraph
    '<p>Zum Abbestellen bitte antworten. Mehr dazu gibt es <a href="https://click.example/x">hier</a>.</p>',
    '<p>Mehr dazu gibt es <a href="https://click.example/x">hier</a>. Abbestellen geht per Antwort.</p>',
    # a line break ends the sentence
    '<td>Newsletter abbestellen<br><a href="https://click.example/x">hier</a> geht es zum Shop</td>',
    # another block
    '<p>Newsletter abbestellen ist einfach</p><p><a href="https://click.example/x">hier</a> zum Shop</p>',
    '<p><a href="https://click.example/x">hier</a> zum Shop</p><div>Newsletter abbestellen</div>',
    # a link with a text of its own is judged by that text
    '<p>Abbestellen kannst du jederzeit, lies dazu <a href="https://click.example/x">unsere ausführlichen '
    'Hinweise zum Datenschutz</a>.</p>',
    # the word belongs to another link (navigation row) or to a URL written out in the text
    '<p><a href="https://ex.org/unsubscribe/1">Abmelden</a> | <a href="https://click.example/x">Home</a></p>',
    '<p>https://ex.org/unsubscribe/1 oder <a href="https://click.example/x">Home</a></p>',
    # not a web link
    '<p>Zum Abbestellen <a href="mailto:x@example.org">hier</a> schreiben.</p>',
])
def test_short_link_outside_the_unsubscribe_sentence_is_not_taken(html):
    assert "https://click.example/x" not in unsub.find_unsubscribe_links(html)
    assert all("mailto" not in link for link in unsub.find_unsubscribe_links(html))


def test_link_found_by_its_own_text_is_not_listed_twice_and_comes_first():
    html = ('<p>Du kannst dich <a href="https://click.example/a">hier</a> abmelden oder '
            '<a href="https://click.example/b">Newsletter abbestellen</a>.</p>')
    assert unsub.find_unsubscribe_links(html) == ["https://click.example/b", "https://click.example/a"]
