"""Which address did the newsletter go to? Taken from the quoted header of a forwarded mail."""

import pytest

from forwarded import original_recipient

ALLOWED = ("wagnersnetz.de", "gmail.com")


def test_german_forward_header():
    text = "---------- Weitergeleitete Nachricht ---------\nVon: News <news@shop.example>\nAn: Philipp <philipp@wagnersnetz.de>\nBetreff: Angebot\n"
    assert original_recipient(text, "jens@wagnersnetz.de", ALLOWED) == "philipp@wagnersnetz.de"


def test_english_forward_header_in_html():
    html = "<div>From: news@shop.example<br>To: <a href='mailto:Some.One@Gmail.com'>Some.One@Gmail.com</a><br>Subject: x</div>"
    assert original_recipient(html, "philipp@wagnersnetz.de", ALLOWED) == "some.one@gmail.com"


def test_quoted_prefix_and_several_recipients_first_allowed_wins():
    text = "> To: other@elsewhere.example, anne@wagnersnetz.de\n"
    assert original_recipient(text, "", ALLOWED) == "anne@wagnersnetz.de"


def test_falls_back_to_forwarder():
    assert original_recipient("no header here", "Jens <jens@wagnersnetz.de>", ALLOWED) == "jens@wagnersnetz.de"


def test_foreign_domain_is_never_used():
    text = "To: victim@elsewhere.example\n"
    assert original_recipient(text, "someone@elsewhere.example", ALLOWED) == ""


@pytest.mark.parametrize("domain", ["evilwagnersnetz.de", "wagnersnetz.de.evil.example"])
def test_domain_must_match_exactly(domain):
    assert original_recipient(f"To: x@{domain}\n", "", ALLOWED) == ""


def test_subdomain_is_not_allowed_implicitly():
    assert original_recipient("To: x@mail.wagnersnetz.de\n", "", ALLOWED) == ""
