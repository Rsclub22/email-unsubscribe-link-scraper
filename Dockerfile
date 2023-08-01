# Verwende das offizielle Python-Image als Basis
FROM python:3.8-slim

# Setze das Arbeitsverzeichnis innerhalb des Containers
WORKDIR /app

# Installiere die erforderlichen Abhängigkeiten
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiere die Anwendungsdateien in den Container
COPY . .

# Starte die Anwendung, wenn der Container gestartet wird
CMD ["python", "app.py"]
