import imaplib
import email
import re
import os
from flask import Flask, render_template, request, redirect, url_for
from dotenv import load_dotenv

app = Flask(__name__)
load_dotenv()  # Laden der Umgebungsvariablen aus der .env-Datei

# Email-Server-Konfiguration
EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_SERVER = os.environ.get("EMAIL_SERVER")

template_dir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, template_folder=template_dir)

def get_text_from_email(email_message):
    text = ""
    for part in email_message.walk():
        content_type = part.get_content_type()
        content_disposition = str(part.get("Content-Disposition"))

        if "text" in content_type and "attachment" not in content_disposition:
            body = part.get_payload(decode=True)
            if body:
                text += body.decode(errors="ignore")
    return text

def find_unsubscribe_links(text):
    # Erweitere die Liste der Abmeldelinks-Patterns bei Bedarf
    unsubscribe_patterns = [
        r"(?i)\babbestellen\b",      # Deutsch - abbestellen
        r"(?i)\babbestellung\b",    # Deutsch - abbestellung
        r"(?i)\bdeabonnieren\b",    # Deutsch - deabonnieren
        r"(?i)\bunsubscribe\b",     # Englisch - unsubscribe
    ]
    unsubscribe_links = []
    for pattern in unsubscribe_patterns:
        matches = re.findall(pattern, text)
        if matches:
            for match in matches:
                link = find_link_in_text(text)
                if link:
                    unsubscribe_links.append(link)
    return unsubscribe_links

def find_link_in_text(text):
    # Einen einfachen Ansatz zur Link-Erkennung in Texten (nicht perfekt)
    link_pattern = r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
    link_matches = re.findall(link_pattern, text)
    return link_matches[0] if link_matches else None

def get_subject_from_email(email_message):
    subject = email_message.get("Subject")
    return subject

def move_emails_to_trash(email_ids):

    username = EMAIL_USERNAME
    password = EMAIL_PASSWORD
    server = EMAIL_SERVER

    mail = imaplib.IMAP4_SSL(server)
    mail.login(username, password)
    mail.select("inbox")

    for email_id in email_ids:
        # Verschiebe die E-Mail in den Papierkorb (Trash)
        mail.copy(email_id, "Trash")
        # Setze die \Deleted-Flagge, um die E-Mail später zu löschen (für Mailcow)
        mail.store(email_id, "+FLAGS", "\\Deleted")

    # Speichere die Änderungen auf dem IMAP-Server
    mail.expunge()
    mail.logout()

@app.route("/", methods=["GET", "POST"])
def index():
    return render_template("index.html", email_address=EMAIL_USERNAME)

@app.route("/result")
def result():

    username = EMAIL_USERNAME
    password = EMAIL_PASSWORD
    server = EMAIL_SERVER

    mail = imaplib.IMAP4_SSL(server)
    mail.login(username, password)
    mail.select("inbox")

    _, messages = mail.search(None, "ALL")
    links_data = {}

    for message_num in messages[0].split():
        _, msg_data = mail.fetch(message_num, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        body = get_text_from_email(msg)
        subject = get_subject_from_email(msg)
        if body:
            unsubscribe_links = find_unsubscribe_links(body)
            if unsubscribe_links:
                links_data[message_num.decode()] = {
                    "subject": subject,
                    "links": list(set(unsubscribe_links))
                }

    mail.logout()

    return render_template("result.html", links_data=links_data)

@app.route("/move-to-trash", methods=["POST"])
def move_to_trash():
    if request.method == "POST":
        email_id = request.form.get("email_id")
        if email_id:
            move_emails_to_trash([email_id])
    return redirect(url_for("result"))

@app.route("/move-all-to-trash", methods=["POST"])
def move_all_to_trash():
    if request.method == "POST":
        email_ids = request.form.getlist("email_id")
        if email_ids:
            move_emails_to_trash(email_ids)
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)