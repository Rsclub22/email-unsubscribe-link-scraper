"""
E-Mail Unsubscribe Link Finder.

Reads a collection mailbox (forwarded newsletters), lists the unsubscribe links per mail and moves
handled mails to the trash. Runs behind the reverse proxy (Caddy forward_auth / Authelia); the
mailbox credentials come from the environment.
"""

import email
import imaplib
import logging
import os
import re
from email.header import decode_header, make_header

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, url_for

from oauth import MailboxOAuth, OAuthError, xoauth2_string

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

UNSUBSCRIBE_PATTERN = re.compile(
    r"\babbestellen\b|\bunsubscribe\b|\bdeabonnieren\b|\babbestellung\b|\babmelden\b", re.IGNORECASE
)
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
UID_PATTERN = re.compile(r"^\d+$")


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


def find_unsubscribe_links(text: str) -> list[str]:
    """Unsubscribe links from HTML anchors (link text or URL matches) and from plain-text URLs."""
    links: list[str] = []
    soup = BeautifulSoup(text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if UNSUBSCRIBE_PATTERN.search(str(anchor)) and is_safe_link(href) and href not in links:
            links.append(href)
    for url in URL_PATTERN.findall(text):
        if UNSUBSCRIBE_PATTERN.search(url) and url not in links:
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


@app.route("/result")
def result():
    mail = connect()
    try:
        links_data = collect_links(mail)
    finally:
        mail.logout()
    return render_template("result.html", links_data=links_data)


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
