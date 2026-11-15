"""Find the address a forwarded newsletter was originally sent to."""

import re
from email.utils import parseaddr

from bs4 import BeautifulSoup

_HEADER = re.compile(r"^[\s>]*(?:An|To)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# "Name <user@host>" in plain text would otherwise be eaten as an HTML tag by BeautifulSoup.
_ANGLE_ADDRESS = re.compile(r"<([^<>\s]+@[^<>\s]+)>")


def _allowed(address: str, entries) -> bool:
    """An entry is a domain (every address of it is allowed) or a full address (exactly that one is allowed).
    Public mail providers belong in the list as full addresses: a domain entry for them would allow strangers."""
    entries = {e.strip().lower() for e in entries}
    return address in entries or address.rsplit("@", 1)[-1] in entries   # a domain entry never contains "@"


def original_recipient(text: str, forwarder: str, allowed_domains) -> str:
    """First allowed address from the quoted 'To:'/'An:' lines, else the forwarder, else ''.

    allowed_domains holds domains and full addresses (see _allowed). Only allowed addresses are ever returned:
    the agent types this address into foreign pages."""
    plain = BeautifulSoup(_ANGLE_ADDRESS.sub(r"[\1]", text or ""), "html.parser").get_text("\n")
    for line in _HEADER.findall(plain):
        for address in _ADDRESS.findall(line):
            if _allowed(address.lower(), allowed_domains):
                return address.lower()
    sender = parseaddr(forwarder or "")[1].lower()
    return sender if _ADDRESS.fullmatch(sender) and _allowed(sender, allowed_domains) else ""
