"""HTTP service of the unsubscribe agent: POST /run {"url": ..., "address": ...} -> result of one case."""

import logging
import os
import threading

from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright

from llm import LLM
from runner import run_case

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)
LOCK = threading.Lock()   # one case at a time
MAX_STEPS = int(os.environ.get("MAX_STEPS", "20"))
CASE_TIMEOUT = float(os.environ.get("CASE_TIMEOUT", "300"))


def _llm() -> LLM:
    return LLM(os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ["LLM_API_KEY"])


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(silent=True) or {}
    url, address = data.get("url"), data.get("address", "")
    if not isinstance(url, str) or not isinstance(address, str):
        return jsonify({"error": "url and address must be strings"}), 400
    with LOCK, sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
        try:
            result = run_case(browser, url, address, _llm(), max_steps=MAX_STEPS, case_timeout=CASE_TIMEOUT)
        finally:
            browser.close()
    logger.info("case finished: %s (%s), %d step(s), stage %d", result["result"], result["reason"], result["steps"],
                result["stage"])   # never log the url or the address: both are personal data of the users
    return jsonify(result)
