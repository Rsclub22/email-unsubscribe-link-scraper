"""
E-Mail Unsubscribe Link Finder.

Reads a collection mailbox (forwarded newsletters), lists the unsubscribe links per mail and moves
handled mails to the trash. Runs behind the reverse proxy (Caddy forward_auth / Authelia); the
mailbox credentials come from the environment.
"""

import datetime
import email
import html
import imaplib
import json
import logging
import os
import re
import threading
from email.header import decode_header, make_header

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag
from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, url_for

from cases import CaseStore
from oauth import MailboxOAuth, OAuthError, xoauth2_string
from worker import AgentUnavailable, ScanFailed, Worker, _tick, start_scheduler

load_dotenv()  # optional .env for local runs

EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_SERVER = os.environ.get("EMAIL_SERVER", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "993"))
EMAIL_FOLDER = os.environ.get("EMAIL_FOLDER", "INBOX")
TRASH_FOLDER = os.environ.get("TRASH_FOLDER", "Trash")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

template_dir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, template_folder=template_dir)

# OAuth (SSO) for the collection mailbox if OIDC_* and TOKEN_KEY are set; otherwise EMAIL_PASSWORD
OAUTH = MailboxOAuth.from_env()

# Unsubscribe agent: cases live in DATA_DIR/cases.db; without AGENT_URL they are only recorded and shown
DATA_DIR = os.environ.get("DATA_DIR", "")
AGENT_URL = os.environ.get("AGENT_URL", "").rstrip("/")
AGENT_CASE_TIMEOUT = float(os.environ.get("AGENT_CASE_TIMEOUT", "300"))
DONE_FOLDER = os.environ.get("DONE_FOLDER", "Erledigt")

UNSUBSCRIBE_PATTERN = re.compile(
    r"\babbestellen\b|\bunsubscribe\b|\bdeabonnieren\b|\babbestellung\b|\babmelden\b", re.IGNORECASE
)
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
UID_PATTERN = re.compile(r"^\d+$")
# Characters that end a sentence or close a quote or bracket in running text, not a URL.
TRAILING_PUNCTUATION = ".,;:!?)\"'\u201d\u2019\u00bb\u203a"
# A link with a text this short ("hier", "click here") says nothing itself: the sentence around it decides.
SHORT_LINK_TEXT = 25
SENTENCE_END = re.compile(r"[.!?\u2026](?:\s|$)")
# Tags that stand inside a sentence; every other tag (br, p, td, div, ...) ends it.
INLINE_TAGS = frozenset("a abbr b big code del em font i ins mark nobr s small span strike strong sub sup tt u wbr".split())


def is_safe_link(url: str) -> bool:
    """Only plain web links: mails are untrusted, javascript:/data: links must never become clickable."""
    return url.lower().startswith(("https://", "http://"))


def get_text_from_email(message) -> str:
    text = ""
    for part in message.walk():
        disposition = str(part.get("Content-Disposition"))
        if "text" in part.get_content_type() and "attachment" not in disposition:
            body = part.get_payload(decode=True)
            if body:
                text += body.decode(errors="ignore")
    return text


def get_subject(message) -> str:
    raw = message.get("Subject", "")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _sentence_around(anchor) -> str:
    """The sentence a link stands in, without the link text: what is written before and after it, up to a full
    stop, a line break or the edge of the paragraph or table cell."""
    own = {id(parent) for parent in anchor.parents} | {id(anchor)}

    def side(elements) -> list[str]:
        parts = []
        for element in elements:
            if isinstance(element, Tag):
                if element.name not in INLINE_TAGS:
                    break
                continue
            parents = [parent for parent in element.parents if id(parent) not in own]
            if id(anchor) in {id(parent) for parent in element.parents}:   # the link's own text
                continue
            if any(parent.name not in INLINE_TAGS for parent in parents):   # text of a neighbouring block
                break
            if any(parent.name == "a" for parent in parents):   # another link's text speaks for that link only
                continue
            parts.append(str(element))
        return parts

    before = " ".join("".join(reversed(side(anchor.previous_elements))).split())
    after = " ".join("".join(side(anchor.next_elements)).split())
    # a URL written out in the text is a link of its own (see below), not a word of this sentence
    before, after = URL_PATTERN.sub(" ", before), URL_PATTERN.sub(" ", after)
    return SENTENCE_END.split(before + " ")[-1] + " " + SENTENCE_END.split(after + " ")[0]


def find_unsubscribe_links(text: str) -> list[str]:
    """Unsubscribe links from HTML anchors (link text or URL matches, or a short link in a sentence that matches)
    and from plain-text URLs."""
    links: list[str] = []
    soup = BeautifulSoup(text, "html.parser")
    anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        href = str(anchor["href"]).strip()
        if UNSUBSCRIBE_PATTERN.search(str(anchor)) and is_safe_link(href) and href not in links:
            links.append(href)
    for anchor in anchors:   # "you can unsubscribe <a>here</a>": the word stands next to the link
        href = str(anchor["href"]).strip()
        label = " ".join(anchor.get_text(" ").split())
        if (href not in links and is_safe_link(href) and 0 < len(label) <= SHORT_LINK_TEXT
                and UNSUBSCRIBE_PATTERN.search(_sentence_around(anchor))):
            links.append(href)
    for url in URL_PATTERN.findall(text):
        # the text search also runs over raw HTML: unescape (&amp;) like the parser does for hrefs, so the same
        # link is not taken twice, and drop punctuation that follows a link in running text
        url = html.unescape(url).rstrip(TRAILING_PUNCTUATION)
        if UNSUBSCRIBE_PATTERN.search(url) and is_safe_link(url) and url not in links:
            links.append(url)
    return links


def connect() -> imaplib.IMAP4_SSL:
    token = OAUTH.access_token() if OAUTH else None   # before connecting: fail fast without a token
    mail = imaplib.IMAP4_SSL(EMAIL_SERVER, EMAIL_PORT)
    if token:
        mail.authenticate("XOAUTH2", lambda _: xoauth2_string(EMAIL_USERNAME, token).encode())
    else:
        mail.login(EMAIL_USERNAME, EMAIL_PASSWORD)
    mail.select(EMAIL_FOLDER)
    return mail


def collect_links(mail) -> dict:
    """{uid: {"subject": ..., "links": [...]}} for all mails with unsubscribe links (UIDs stay stable)."""
    _, data = mail.uid("search", None, "ALL")
    result = {}
    for uid in data[0].split():
        _, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
        if not msg_data or not isinstance(msg_data[0], tuple):
            continue
        message = email.message_from_bytes(msg_data[0][1])
        body = get_text_from_email(message)
        links = find_unsubscribe_links(body) if body else []
        if links:
            result[uid.decode()] = {"subject": get_subject(message), "links": links}
    return result


def move_emails_to_trash(uids: list[str]) -> int:
    uids = [u for u in uids if UID_PATTERN.match(u)]
    if not uids:
        return 0
    mail = connect()
    try:
        for uid in uids:
            # Mailcow/Dovecot: copy to trash, flag deleted, expunge
            mail.uid("copy", uid, TRASH_FOLDER)
            mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
        mail.expunge()
    finally:
        mail.logout()
    logger.info("Moved %d mail(s) to %s", len(uids), TRASH_FOLDER)
    return len(uids)


def parse_mail(message) -> tuple[str, list[str], str]:
    text = get_text_from_email(message)
    return get_subject(message), find_unsubscribe_links(text) if text else [], text


def call_agent(url: str, address: str) -> dict:
    """Hand one link to the agent container. It gets the link and the address, nothing else."""
    try:
        # the agent takes up to AGENT_CASE_TIMEOUT per link; leave room for browser start and shutdown
        r = httpx.post(AGENT_URL + "/run", json={"url": url, "address": address},
                       timeout=2 * AGENT_CASE_TIMEOUT + 60)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise AgentUnavailable(f"agent not reachable: {type(e).__name__}") from None


def _env_list(name: str) -> list[str]:
    return [v.strip().lower() for v in os.environ.get(name, "").split(",") if v.strip()]


STORE = CaseStore(os.path.join(DATA_DIR, "cases.db")) if DATA_DIR else None
WORKER = None
if STORE:
    STORE.recover()
    WORKER = Worker(
        STORE, lambda: connect(), parse_mail, call_agent if AGENT_URL else None,
        inbox=EMAIL_FOLDER, done_folder=DONE_FOLDER,
        retention_days=int(os.environ.get("RETENTION_DAYS", "30")),
        max_cases=int(os.environ.get("AGENT_MAX_CASES", "10")),
        max_deferrals=int(os.environ.get("AGENT_MAX_DEFERRALS", "12")),
        allowed_domains=_env_list("AGENT_ALLOWED_DOMAINS"),
        allowed_senders=_env_list("AGENT_ALLOWED_SENDERS"),
    )
    if os.environ.get("AGENT_SCHEDULER", "1") == "1":
        start_scheduler(WORKER, 60 * int(os.environ.get("AGENT_INTERVAL_MINUTES", "10")))


def scan_inbox() -> dict:
    """{case key: uid} of the mails currently in the inbox; creates cases for new mails."""
    mail = connect()
    try:
        return WORKER.scan(mail)[0]
    finally:
        mail.logout()


def start_run() -> threading.Thread:
    """One run in the background (it can take many minutes); _tick notes any error instead of raising."""
    thread = threading.Thread(target=_tick, args=(WORKER,), name="unsubscribe-run-now", daemon=True)
    thread.start()
    return thread


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        email_address=EMAIL_USERNAME,
        oauth=OAUTH is not None,
        connected=OAUTH.connected() if OAUTH else True,
        error=request.args.get("error", ""),
    )


@app.route("/connect")
def connect_mailbox():
    """Connect the collection mailbox: sign in at the SSO as its technical account (private window)."""
    if not OAUTH:
        abort(404)
    return redirect(OAUTH.start())


@app.route("/oidc/callback")
def oidc_callback():
    if not OAUTH:
        abort(404)
    if request.args.get("error"):
        return render_template("index.html", email_address=EMAIL_USERNAME, oauth=True, connected=False,
                               error=request.args["error"]), 400
    try:
        OAUTH.finish(request.args.get("state", ""), request.args.get("code", ""))
    except OAuthError as e:
        logger.warning("Mailbox connect failed: %s", e)
        return render_template("index.html", email_address=EMAIL_USERNAME, oauth=True, connected=False,
                               error=str(e)), 400
    logger.info("Collection mailbox %s connected via OAuth", EMAIL_USERNAME)
    # the browser session now belongs to the technical account, which forward_auth does not let in:
    # confirm here instead of redirecting to the start page
    return (
        "<!doctype html><meta charset=utf-8><title>Connected</title>"
        "<p>Collection mailbox connected. Close this private window and open the Unsubscriber again "
        "in your normal browser.</p>"
    )


@app.route("/disconnect", methods=["POST"])
def disconnect_mailbox():
    if not OAUTH:
        abort(404)
    OAUTH.disconnect()
    return redirect(url_for("index"))


@app.errorhandler(OAuthError)
def oauth_error(e):
    logger.warning("Mailbox access failed: %s", e)
    return redirect(url_for("index", error="Mailbox not connected or token expired – connect again"))


STATE_LABELS = {"new": "new", "running": "running", "unsubscribed": "unsubscribed",
                "not_possible": "not possible", "unclear": "unclear"}


@app.route("/result")
def result():
    if WORKER is None:
        mail = connect()
        try:
            links_data = collect_links(mail)
        finally:
            mail.logout()
        return render_template("result.html", links_data=links_data)
    try:
        uids, scan_failed = scan_inbox(), False
    except ScanFailed:   # show what is stored; without the inbox no mail can be offered for trash or retry
        uids, scan_failed = {}, True
    cases = [dict(c, uid=uids[c["key"]].decode() if c["key"] in uids else "", label=STATE_LABELS[c["state"]])
             for c in STORE.all()]
    last = json.loads(STORE.meta("last_run", "{}"))
    at = STORE.meta("last_run_at")
    return render_template(
        "cases.html", cases=cases, last=last, agent=bool(AGENT_URL), paused=STORE.meta("paused") == "1",
        scan_failed=scan_failed,
        last_at=datetime.datetime.fromtimestamp(int(at)).strftime("%Y-%m-%d %H:%M") if at else "never")


@app.route("/run-now", methods=["POST"])
def run_now():
    if WORKER is None:
        abort(404)
    start_run()
    return redirect(url_for("result"))


@app.route("/retry", methods=["POST"])
def retry():
    if STORE is None:
        abort(404)
    STORE.retry(request.form.get("key", ""))
    return redirect(url_for("result"))


@app.route("/pause", methods=["POST"])
def pause():
    if STORE is None:
        abort(404)
    STORE.set_meta("paused", "1" if request.form.get("paused") == "1" else "0")
    return redirect(url_for("result"))


@app.route("/move-to-trash", methods=["POST"])
def move_to_trash():
    email_id = request.form.get("email_id", "")
    move_emails_to_trash([email_id])
    return redirect(url_for("result"))


@app.route("/move-all-to-trash", methods=["POST"])
def move_all_to_trash():
    move_emails_to_trash(request.form.getlist("email_id"))
    return redirect(url_for("index"))


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    # local development only; the container runs gunicorn (no debugger)
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
