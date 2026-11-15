"""Checks on a page snapshot that need no model: success texts, required input, login pages."""

import re
from urllib.parse import urlsplit

_SUCCESS = re.compile(
    r"(you('ve| have)? (been |successfully |now )*unsubscribed"
    r"|you are (now )?unsubscribed"
    r"|unsubscribe(d)? success(ful|fully)"
    r"|successfully unsubscribed"
    r"|you will no longer receive"
    r"|(wurden?|wurdest) (erfolgreich )?(vom newsletter )?abgemeldet"
    r"|erfolgreich (vom newsletter |von unserem newsletter )?abgemeldet"
    r"|abmeldung (war |ist )?(erfolgreich|bestätigt)"
    r"|erfolgreich abbestellt"
    r"|erhalten (ab sofort |künftig |in zukunft )?keine (e-?mails|newsletter|nachrichten) mehr)",
    re.IGNORECASE)
_CONFIRM = re.compile(
    r"(abmelden|abbestellen|bestätigen|absenden|speichern|austragen|unsubscribe|confirm|submit|save|opt.?out|update)",
    re.IGNORECASE)
_LOGIN_PATH = re.compile(r"(^|/)(login|log-in|signin|sign-in|sign_in|anmelden|einloggen|auth)(/|$|\.)", re.IGNORECASE)


def looks_unsubscribed(text: str) -> bool:
    return bool(_SUCCESS.search(text or ""))


def needs_input(elements: list[dict]) -> bool:
    """True if the page still offers something to fill in, or a button or link that looks like a confirmation.

    Clickable non-form elements (links, buttons, elements with a role such as button) count by their label. The
    snapshot does not record the role: such elements are listed with their own tag (div, span, ...)."""
    for e in elements:
        if e["tag"] in ("select", "textarea"):
            return True
        if e["tag"] == "input" and e["type"] not in ("button", "submit", "reset", "image"):
            return True
        if (e["tag"] != "input" or e["type"] in ("button", "submit")) and _CONFIRM.search(e.get("label", "")):
            return True
    return False


def has_password(elements: list[dict]) -> bool:
    return any(e["tag"] == "input" and e["type"] == "password" for e in elements)


def is_login_url(url: str) -> bool:
    return bool(_LOGIN_PATH.search(urlsplit(url).path))
