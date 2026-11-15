"""HTTP service of the unsubscribe agent: POST /run {"url": ..., "address": ...} -> result of one case."""

import logging
import os
import threading

from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright

from llm import LLM
from runner import BROWSER_ARGS, CODE_REASONS, run_case

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def _required(name: str) -> str:
    """A setting the service cannot work without: a missing one stops the start instead of failing every case."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set: the agent cannot reach the model without it")
    return value


MODEL = LLM(_required("LLM_BASE_URL"), _required("LLM_MODEL"), _required("LLM_API_KEY"))   # one at a time: shared
MAX_STEPS = int(os.environ.get("MAX_STEPS", "20"))
CASE_TIMEOUT = float(os.environ.get("CASE_TIMEOUT", "300"))

app = Flask(__name__)
LOCK = threading.Lock()   # one case at a time


def _failure() -> dict:
    """Result for a case the agent itself failed on. Unclear, not error: the attempt is used up, so one bad page
    cannot block the queue of the caller."""
    return {"result": "unclear", "reason": "agent_failure", "evidence": "", "evidence_url": "", "steps": 0, "stage": 1}


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    url, address = data.get("url"), data.get("address", "")
    if not isinstance(url, str) or not isinstance(address, str):
        return jsonify({"error": "url and address must be strings"}), 400
    with LOCK:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=BROWSER_ARGS)
                try:
                    result = run_case(browser, url, address, MODEL, max_steps=MAX_STEPS, case_timeout=CASE_TIMEOUT)
                finally:
                    browser.close()
        except Exception as e:
            # only the type: messages of browser and library errors may contain the url
            logger.warning("case failed in the agent: %s", type(e).__name__)
            result = _failure()
    # never log the url, the address or a reason the model wrote: all may contain personal data of the users
    category = result["reason"] if result["reason"] in CODE_REASONS else "model"
    logger.info("case finished: %s (%s), %d step(s), stage %d", result["result"], category, result["steps"],
                result["stage"])
    return jsonify(result)
