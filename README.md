# Email Unsubscribe Links Scraper

This is a simple Python web application that logs into an email server, scans the inbox for unsubscribe links, and displays the results in a table. It also provides the option to move selected emails to the trash.

## Disclaimer
This App is still in development

## How to use

1. Clone this repository.
2. Build the Docker image:
```docker build -t email-unsubscribe-links-scraper .```
3. Run the Docker container:
```docker run -p 5000:5000 -e EMAIL_USERNAME=your_email@example.com -e EMAIL_PASSWORD=your_email_password -e EMAIL_SERVER=your_email_server email-unsubscribe-links-scraper```
4. Access the application in your web browser at http://localhost:5000.

### For non-build-use:
Run following command: ```docker run -p 5000:5000 -e EMAIL_USERNAME=your_email@example.com -e EMAIL_PASSWORD=your_email_password -e EMAIL_SERVER=your_email_server rsclub22/email-unsubscribe-links-scraper```

## License

Copyright 2023 Philipp Wagner

This project is licensed under the GPLv3 License - see the [LICENSE](LICENSE) file for details.

## Betrieb

Konfiguration per Umgebung: `EMAIL_SERVER`, `EMAIL_PORT` (993), `EMAIL_USERNAME`, `EMAIL_PASSWORD`
(Sammelpostfach), optional `EMAIL_FOLDER` (`INBOX`) und `TRASH_FOLDER` (`Trash`).
Statt `EMAIL_PASSWORD` geht SSO (OAuth/XOAUTH2): `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`,
`OIDC_REDIRECT_URI` (`https://<host>/oidc/callback`), `TOKEN_KEY` (Fernet-Schlüssel) und `DATA_DIR` (`/data`).
Dann zeigt die Startseite „Connect mailbox“: einmal im privaten Fenster als technisches Konto des
Sammelpostfachs am SSO anmelden; das Refresh-Token liegt verschlüsselt in `DATA_DIR/oauth-token`.
Der Container startet gunicorn auf Port 5000 (kein Flask-Debugger) und gehört hinter einen
Reverse Proxy mit Anmeldung (bei Wagnersnetz Caddy `forward_auth` über Authelia).
Tests: `pytest tests`.
