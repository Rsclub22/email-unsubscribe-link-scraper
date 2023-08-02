import imaplib
import email
import re
import os
from flask import Flask, render_template, request, redirect, url_for
from dotenv import load_dotenv
from bs4 import BeautifulSoup

app = Flask(__name__)
load_dotenv()  # Laden der Umgebungsvariablen aus der .env-Datei

# Email-Server-Konfiguration
EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_SERVER = os.environ.get("EMAIL_SERVER")

template_dir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, template_folder=template_dir)
link_pattern = r"<a href=\"(.*?)\">(.*?)</a>"
unsubscribe_pattern = r"(?i)\babbestellen\b|\bunsubscribe\b|\bdeabonnieren\b|\babbestellung\b|\babmelden\b"

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

def find_unsubscribe_links(text, html_links):
    soup = BeautifulSoup(text, 'html.parser')
    unsubscribe_links = []


    for link in soup.find_all('a', href=True):
        if re.search(unsubscribe_pattern, str(link)) and str(link['href']) not in html_links:
            print(link['href'])
            test1=str(link['href'])
            print(test1)
            if test1 not in unsubscribe_links:
                print(test1)
                unsubscribe_links.append(test1)
                

    # Zusätzlich nach einfachen URLs suchen und auf das Unsubscribe-Pattern prüfen
    simple_urls = re.findall(r"(https?://\S+)", text)
    for url in simple_urls:
        if re.search(unsubscribe_pattern, url) and url not in html_links:
            if url not in unsubscribe_links:
                unsubscribe_links.append(url)

    return unsubscribe_links

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
    html_links = set()

    for message_num in messages[0].split():
        _, msg_data = mail.fetch(message_num, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        body = get_text_from_email(msg)
        subject = get_subject_from_email(msg)
        if body:
            unsubscribe_links_in_html = find_unsubscribe_links(body, html_links)
            html_links.update(unsubscribe_links_in_html)
            unsubscribe_links_in_text = find_unsubscribe_links(body, html_links)
            all_unsubscribe_links = list(set(unsubscribe_links_in_html + unsubscribe_links_in_text))
            if all_unsubscribe_links:
                links_data[message_num.decode()] = {
                    "subject": subject,
                    "links": all_unsubscribe_links
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