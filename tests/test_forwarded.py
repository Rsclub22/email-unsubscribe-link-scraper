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


def test_malformed_forwarder_is_not_used():
    assert original_recipient("no header", "a@b@wagnersnetz.de", ALLOWED) == ""
    assert original_recipient("no header", "<x y@wagnersnetz.de>", ALLOWED) == ""


def test_full_address_entry_allows_only_that_address():
    allowed = ("wagnersnetz.de", "Some.One@Gmail.com")
    assert original_recipient("To: some.one@gmail.com\n", "", allowed) == "some.one@gmail.com"
    assert original_recipient("To: SOME.ONE@GMAIL.COM\n", "", allowed) == "some.one@gmail.com"
    assert original_recipient("To: someone.else@gmail.com\n", "", allowed) == ""


def test_body_line_with_a_third_persons_public_address_is_refused():
    text = ("Von: news@shop.example\nAn: philipp@gmail.com\n\nWrite to us!\nTo: someone@gmail.com\n")
    allowed = ("wagnersnetz.de", "philipp@gmail.com")
    assert original_recipient("To: someone@gmail.com\n", "philipp@gmail.com", allowed) == "philipp@gmail.com"
    assert original_recipient(text, "", allowed) == "philipp@gmail.com"


def test_mixed_list_of_domains_and_addresses():
    allowed = ("wagnersnetz.de", "anne@gmail.com")
    assert original_recipient("To: x@elsewhere.example, jens@wagnersnetz.de\n", "", allowed) == "jens@wagnersnetz.de"
    assert original_recipient("To: bob@gmail.com, anne@gmail.com\n", "", allowed) == "anne@gmail.com"
    assert original_recipient("no header", "anne@gmail.com", allowed) == "anne@gmail.com"
    assert original_recipient("no header", "bob@gmail.com", allowed) == ""


def test_address_entry_does_not_allow_its_domain_or_a_lookalike():
    allowed = ("anne@gmail.com",)
    assert original_recipient("To: gmail.com@gmail.com\n", "", allowed) == ""
    assert original_recipient("To: xanne@gmail.com\n", "", allowed) == ""
    assert original_recipient("To: anne@gmail.com.evil.example\n", "", allowed) == ""
