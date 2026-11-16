"""Checks on a page snapshot that need no model."""

import pytest

from pagecheck import has_password, is_login_url, looks_unsubscribed, needs_input


@pytest.mark.parametrize("text", [
    "You have been unsubscribed.", "You have successfully unsubscribed from our newsletter",
    "Sie wurden erfolgreich abgemeldet", "Ihre Abmeldung war erfolgreich", "Du wurdest abgemeldet.",
    "Sie haben sich erfolgreich vom Newsletter abgemeldet", "Unsubscribe successful", "You are now unsubscribed",
    "Sie erhalten ab sofort keine E-Mails mehr", "You will no longer receive emails from us",
    "Abmeldung bestätigt"])
def test_success_texts(text):
    assert looks_unsubscribed("Header\n" + text + "\nFooter") is True


@pytest.mark.parametrize("text", [
    "Unsubscribe", "Möchten Sie sich wirklich abmelden?", "Click the button to unsubscribe",
    "Are you sure you want to unsubscribe?", "Bitte bestätigen Sie Ihre Abmeldung", "Newsletter abbestellen", ""])
def test_no_false_success(text):
    assert looks_unsubscribed(text) is False


def el(tag, type_="", label=""):
    return {"n": 1, "tag": tag, "type": type_, "label": label}


def test_needs_input_for_fields_and_confirm_buttons():
    assert needs_input([el("button", "submit", "Abmelden bestätigen")]) is True
    assert needs_input([el("input", "email", "E-Mail")]) is True
    assert needs_input([el("select", "", "Grund")]) is True
    assert needs_input([el("input", "checkbox", "Alle Newsletter")]) is True


def test_no_input_needed_for_plain_links_and_unrelated_buttons():
    assert needs_input([el("a", "", "Startseite"), el("button", "", "Cookie-Einstellungen"), el("a", "", "Impressum")]) is False
    assert needs_input([]) is False


@pytest.mark.parametrize("element", [el("a", "", "Abmeldung bestätigen"), el("a", "", "Unsubscribe"),
                                     el("div", "", "Abmelden bestätigen"), el("span", "", "Confirm")])
def test_confirmation_offered_as_a_link_or_role_button_needs_input(element):
    assert needs_input([el("a", "", "Startseite"), element]) is True


def test_success_text_with_a_confirmation_link_is_no_stage_1_success():
    text = "Abmeldung bestätigt? Bitte klicken Sie unten."
    elements = [el("a", "", "Abmeldung bestätigen"), el("a", "", "Impressum")]
    assert looks_unsubscribed(text) is True and needs_input(elements) is True


def test_password_field_detected():
    assert has_password([el("input", "text", "User"), el("input", "password", "Passwort")]) is True
    assert has_password([el("input", "text", "User")]) is False


@pytest.mark.parametrize("url,expected", [
    ("https://shop.example/login?next=/prefs", True), ("https://accounts.google.com/signin/v2", True),
    ("https://shop.example/account/sign-in", True), ("https://shop.example/anmelden", True),
    ("https://shop.example/unsubscribe/123", False), ("https://shop.example/blog/login-tips-article", False)])
def test_login_urls(url, expected):
    assert is_login_url(url) is expected
