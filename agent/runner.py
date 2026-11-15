"""Browser loop of the unsubscribe agent: stage 1 without the model, stage 2 with one action per step."""

import logging
import time

from egress import EgressProxy
from guard import is_public_url
from llm import LLMUnavailable
from pagecheck import has_password, is_login_url, looks_unsubscribed, needs_input
from protocol import SYSTEM_PROMPT, ProtocolError, build_prompt, parse_action

logger = logging.getLogger(__name__)

MAX_TEXT = 6000
MAX_ELEMENTS = 60
EVIDENCE = 400

# Lists the visible controls and marks each with data-ua-idx so the next action can find it again.
SNAPSHOT_JS = """
([maxElements, maxText]) => {
  document.querySelectorAll('[data-ua-idx]').forEach(e => e.removeAttribute('data-ua-idx'));
  const selector = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=checkbox], [role=radio]';
  const elements = [];
  for (const el of document.querySelectorAll(selector)) {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (box.width < 1 || box.height < 1 || style.visibility === 'hidden' || style.display === 'none') continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && type === 'hidden') continue;
    if (elements.length >= maxElements) break;
    const n = elements.length + 1;
    el.setAttribute('data-ua-idx', String(n));
    let label = el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                el.getAttribute('title') || el.getAttribute('name') || '';
    if (tag === 'input' && (type === 'submit' || type === 'button')) label = el.value || label;
    const wrap = el.closest('label');
    const forLabel = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    if (forLabel) label = forLabel.innerText + ' ' + label;
    else if (wrap && tag !== 'label') label = wrap.innerText + ' ' + label;
    const item = {n, tag, type, label: label.replace(/\\s+/g, ' ').trim().slice(0, 80)};
    if (tag === 'select') item.options = Array.from(el.options).map(o => o.text.trim()).slice(0, 20);
    if (type === 'checkbox' || type === 'radio') item.checked = el.checked;
    elements.push(item);
  }
  const captcha = !!document.querySelector(
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], ' +
    '.g-recaptcha, .h-captcha, .cf-turnstile');
  const text = document.body ? document.body.innerText : '';
  return {title: document.title, text: text.slice(0, maxText), tail: text.replace(/\\s+/g, ' ').trim(), elements, captcha};
}
"""


def _result(result: str, reason: str = "", evidence: str = "", evidence_url: str = "", steps: int = 0, stage: int = 1) -> dict:
    return {"result": result, "reason": reason, "evidence": evidence[-EVIDENCE:], "evidence_url": evidence_url,
            "steps": steps, "stage": stage}


def _settle(page, nav_timeout: float) -> None:
    """Wait for a page change to finish; pages that never go idle are still usable."""
    for state, timeout in (("domcontentloaded", nav_timeout), ("networkidle", 5)):
        try:
            page.wait_for_load_state(state, timeout=timeout * 1000)
        except Exception:
            pass
    page.wait_for_timeout(500)


def _act(page, action, address: str) -> str:
    target = page.locator(f'[data-ua-idx="{action.element}"]').first
    if action.kind == "click":
        target.click(timeout=10000, no_wait_after=True)
    elif action.kind == "select":
        target.select_option(label=action.option, timeout=10000)
    elif action.kind == "check":
        target.set_checked(action.on, timeout=10000)
    else:
        target.fill(address, timeout=10000)
    return f"{action.kind} {action.element}"


def run_case(browser, url: str, address: str, llm, *, max_steps: int = 20, case_timeout: float = 300,
             nav_timeout: float = 30, url_ok=is_public_url, clock=time.monotonic) -> dict:
    """Try to unsubscribe at url. Returns result unsubscribed | not_possible | unclear | error with a reason."""
    if not url_ok(url):
        return _result("not_possible", "blocked_address")
    deadline = clock() + case_timeout
    blocked: list[str] = []
    # Every request leaves through the egress proxy, which checks url_ok also for redirect hops and tunnels;
    # the route gate below sees only the first request of each redirect chain, but with the full https URL.
    proxy = EgressProxy(url_ok, blocked, timeout=nav_timeout).start()
    context = None
    try:
        context = browser.new_context(accept_downloads=False, service_workers="block",
                                      proxy={"server": proxy.address})

        def gate(route):
            if url_ok(route.request.url):
                route.continue_()
            else:
                blocked.append(route.request.url)
                route.abort("blockedbyclient")

        context.route("**/*", gate)
        page = context.new_page()
        context.on("page", lambda extra: extra.close() if extra != page else None)   # pop-ups and new tabs
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("download", lambda download: download.cancel())
        try:
            page.goto(url, timeout=nav_timeout * 1000, wait_until="domcontentloaded")
        except Exception as e:
            reason = "blocked_address" if blocked else "page_error"
            logger.info("first load failed (%s): %s", reason, type(e).__name__)
            return _result("not_possible", reason)
        _settle(page, nav_timeout)

        history: list[str] = []
        bad_replies = 0
        steps = 0
        while True:
            try:
                snap = page.evaluate(SNAPSHOT_JS, [MAX_ELEMENTS, MAX_TEXT])
            except Exception:
                _settle(page, nav_timeout)      # the page was still navigating; one more try
                try:
                    snap = page.evaluate(SNAPSHOT_JS, [MAX_ELEMENTS, MAX_TEXT])
                except Exception:
                    return _result("not_possible", "page_error", steps=steps, stage=2 if steps else 1)
            stage = 2 if steps else 1
            here = page.url
            if not url_ok(here) and here != "about:blank":
                return _result("not_possible", "blocked_address", steps=steps, stage=stage)
            if has_password(snap["elements"]) or is_login_url(here):
                return _result("not_possible", "login", snap["tail"], here, steps, stage)
            if snap["captcha"]:
                return _result("not_possible", "captcha", snap["tail"], here, steps, stage)
            if steps == 0 and looks_unsubscribed(snap["text"]) and not needs_input(snap["elements"]):
                return _result("unsubscribed", "page confirms without interaction", snap["tail"], here, 0, 1)
            if steps >= max_steps or clock() >= deadline:
                return _result("not_possible", "budget", snap["tail"], here, steps, 2)

            steps += 1
            prompt = build_prompt(snap["title"], here, snap["text"], snap["elements"], history, bool(address))
            try:
                reply = llm.ask(SYSTEM_PROMPT, prompt)
            except LLMUnavailable as e:
                logger.warning("model unavailable: %s", e)
                return _result("error", "llm_unavailable", steps=steps - 1, stage=2)
            try:
                action = parse_action(reply, snap["elements"], bool(address))
            except ProtocolError as e:
                bad_replies += 1
                history.append(f"invalid reply ({e})")
                if bad_replies >= 3:
                    return _result("unclear", "three invalid replies from the model in a row", snap["tail"], here, steps, 2)
                continue
            bad_replies = 0
            if action.kind == "done":
                return _result(action.result, action.reason, snap["tail"], here, steps, 2)
            try:
                history.append(_act(page, action, address) + " -> ok")
            except Exception as e:
                history.append(f"{action.kind} {action.element} -> failed ({type(e).__name__})")
            _settle(page, nav_timeout)
    finally:
        if context is not None:
            context.close()
        proxy.stop()
