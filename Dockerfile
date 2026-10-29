FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py oauth.py index.html result.html ./
COPY static ./static

RUN useradd -m -u 1000 app
USER app

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health')"

# gunicorn instead of the Flask development server (no Werkzeug debugger); one worker, because the
# OAuth refresh token rotates and must not be refreshed by two processes at once
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]
