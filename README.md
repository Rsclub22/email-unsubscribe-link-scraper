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

### Unsubscribe agent

With `DATA_DIR` set the app keeps one case per mail in `DATA_DIR/cases.db` and shows them under `/result`.
With `AGENT_URL` (the agent container from `agent/`, `http://<name>:8080`) a background run every
`AGENT_INTERVAL_MINUTES` (10) hands each new case to the agent: it opens the unsubscribe link in a sandboxed
browser, first checks for a confirmation without a model and otherwise lets a model operate the page with five
actions (click, select, check, insert the address, done). Successes are moved to the IMAP folder `DONE_FOLDER`
(`Erledigt`) and deleted after `RETENTION_DAYS` (30); everything else stays in the inbox with a reason.
"Run now" starts a run in the background and returns at once; the result page shows its outcome afterwards.
Further settings: `AGENT_CASE_TIMEOUT` (300), `AGENT_MAX_CASES` (10), `AGENT_ALLOWED_DOMAINS` (comma separated;
only addresses of these domains are ever typed into a page), `AGENT_SCHEDULER=0` (no background run).
`AGENT_ALLOWED_SENDERS` (comma separated) lists the forwarders whose mails become cases; mails from other
senders are ignored. With the agent on and an empty list nothing is processed.

The agent container needs `LLM_BASE_URL` (Open WebUI, `/api/chat/completions` is appended), `LLM_MODEL`,
`LLM_API_KEY`, optionally `MAX_STEPS` (20) and `CASE_TIMEOUT` (300). It must run in a network from which private
addresses are not reachable; the URL guard in `agent/guard.py` is only the first line of defence.
Tests: `pytest tests`, `cd agent && pytest tests` and `sh agent/tests/run-in-docker.sh` (browser tests).
