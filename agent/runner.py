"""Browser loop of the unsubscribe agent: stage 1 without the model, stage 2 with one action per step."""

import asyncio
import inspect
import logging
import threading
import time
from urllib.parse import urlsplit

from egress import EgressProxy
from guard import is_public_url, public_addresses
from llm import LLMUnavailable
from pagecheck import has_password, is_login_url, looks_unsubscribed, needs_input
from protocol import SYSTEM_PROMPT, ProtocolError, build_prompt, parse_action

logger = logging.getLogger(__name__)

# Launch arguments for every browser that runs cases (the service and the tests use the same).
# WebRTC may only use proxied connections: no UDP (STUN/TURN) past the egress proxy.
BROWSER_ARGS = ["--disable-dev-shm-usage", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
WATCHDOG_MARGIN = 2.0       # seconds past the case budget after which the browser context is closed by force
MAX_TEXT = 6000
MAX_ELEMENTS = 60
MAX_LABEL = 80
MAX_OPTIONS = 20
EVIDENCE = 400

# Reasons set by this code; any other reason was written by the model (and may contain personal data).
CODE_REASONS = frozenset({
    "blocked_address", "page_error", "login", "captcha", "budget", "llm_unavailable", "agent_failure",
    "page confirms without interaction", "three invalid replies from the model in a row"})

# Removes the WebRTC constructors before any page script runs (the launch flag above is the second line).
NO_WEBRTC_JS = """
for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel']) {
  try { delete window[name]; } catch (e) {}
  try { Object.defineProperty(window, name, {value: undefined, configurable: false, writable: false}); } catch (e) {}
}
"""

# Lists the visible controls and marks each with data-ua-idx so the next action can find it again.
SNAPSHOT_JS = """
([maxElements, maxText, maxLabel, maxOptions]) => {
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
    const item = {n, tag, type, label: label.replace(/\\s+/g, ' ').trim().slice(0, maxLabel)};
    if (tag === 'select') item.options = Array.from(el.options).map(o => o.text.trim()).slice(0, maxOptions);
    if (tag === 'input' && (type === 'checkbox' || type === 'radio')) item.checked = el.checked;
    elements.push(item);
  }
  const captcha = !!document.querySelector(
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], ' +
    '.g-recaptcha, .h-captcha, .cf-turnstile');
  const text = document.body ? document.body.innerText : '';
  return {title: document.title, text: text.slice(0, maxText), tail: text.replace(/\\s+/g, ' ').trim(), elements, captcha};
}
"""


_SNAPSHOT_KEYS = frozenset({"title", "text", "tail", "elements", "captcha"})
_ELEMENT_KEYS = frozenset({"n", "tag", "type", "label"})
_OPTIONAL_ELEMENT_KEYS = frozenset({"options", "checked"})


class SnapshotError(Exception):
    """The snapshot is not what SNAPSHOT_JS returns: the page tampered with it."""


def check_snapshot(snap) -> dict:
    """The snapshot as SNAPSHOT_JS builds it, checked and cut to its limits again. It is computed in the page's own
    JavaScript world, so a page script can change what it returns (for example by overriding Array.prototype.push).
    Raises SnapshotError for anything else."""
    if type(snap) is not dict or set(snap) != _SNAPSHOT_KEYS:
        raise SnapshotError("not a snapshot")
    if not all(type(snap[k]) is str for k in ("title", "text", "tail")) or type(snap["captcha"]) is not bool:
        raise SnapshotError("snapshot field of the wrong type")
    raw = snap["elements"]
    if type(raw) is not list or len(raw) > MAX_ELEMENTS:
        raise SnapshotError("elements")
    elements = []
    for number, e in enumerate(raw, 1):
        if type(e) is not dict or not _ELEMENT_KEYS <= set(e) <= _ELEMENT_KEYS | _OPTIONAL_ELEMENT_KEYS:
            raise SnapshotError("element keys")
        if type(e["n"]) is not int or e["n"] != number:
            raise SnapshotError("element number")
        if not all(type(e[k]) is str for k in ("tag", "type", "label")):
            raise SnapshotError("element field of the wrong type")
        item = {"n": number, "tag": e["tag"], "type": e["type"], "label": e["label"][:MAX_LABEL]}
        if "options" in e:
            options = e["options"]
            if type(options) is not list or len(options) > MAX_OPTIONS or not all(type(o) is str for o in options):
                raise SnapshotError("element options")
            item["options"] = list(options)
        if "checked" in e:
            if type(e["checked"]) is not bool:
                raise SnapshotError("element checked")
            item["checked"] = e["checked"]
        elements.append(item)
    return {"title": snap["title"], "text": snap["text"][:MAX_TEXT], "tail": snap["tail"], "elements": elements,
            "captcha": snap["captcha"]}


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


def _authority(url: str) -> tuple:
    try:
        parts = urlsplit(url)
        return (parts.hostname, parts.port or {"http": 80, "https": 443}.get(parts.scheme))
    except ValueError:
        return (None, None)


def _password_anywhere(page) -> bool:
    """A visible password field in any frame, not only among the controls listed for the model."""
    for frame in page.frames:
        try:
            if frame.locator("input[type=password]:visible").count():
                return True
        except Exception:
            continue        # detached or crashed frame
    return False


def _ask(llm, prompt: str, timeout: float) -> str:
    """Ask the model with a timeout capped by the remaining budget, if its ask() takes one."""
    try:
        takes_timeout = "timeout" in inspect.signature(llm.ask).parameters
    except (TypeError, ValueError):
        takes_timeout = False
    if takes_timeout:
        return llm.ask(SYSTEM_PROMPT, prompt, timeout=timeout)
    return llm.ask(SYSTEM_PROMPT, prompt)


def _refuse_download(download) -> None:
    logger.info("download refused")
    try:
        download.cancel()
    except Exception:
        pass


def _report_close(future) -> None:
    if not future.cancelled() and future.exception() is not None:
        logger.warning("watchdog could not close the browser context: %s", type(future.exception()).__name__)


class _Watchdog:
    """Hard wall clock for one case. Playwright's sync API may only be used from its own thread, so the timer
    schedules a close on Playwright's event loop: of the case's browser context, or of the whole browser while the
    context does not exist yet. A call that hangs (an endless script blocks page.evaluate forever, or new_context
    never returns) then fails at once, and the case ends with budget."""

    def __init__(self, seconds: float, browser):
        self.expired = threading.Event()
        self.context = None             # set once created; until then the browser is closed instead
        self._browser = browser
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def _fire(self) -> None:
        self.expired.set()
        target = self.context if self.context is not None else self._browser
        try:
            # private parts of Playwright 1.65 (pinned): the async implementation object and its event loop
            future = asyncio.run_coroutine_threadsafe(target._impl_obj.close(None), target._loop)
        except Exception as e:
            logger.warning("watchdog could not close the browser context: %s", type(e).__name__)
            return
        future.add_done_callback(_report_close)

    def cancel(self) -> None:
        self._timer.cancel()


def run_case(browser, url: str, address: str, llm, *, max_steps: int = 20, case_timeout: float = 300,
             nav_timeout: float = 30, url_ok=is_public_url, addresses_for=public_addresses, clock=time.monotonic,
             blocked_log: list | None = None) -> dict:
    """Try to unsubscribe at url. Returns result unsubscribed | not_possible | unclear | error with a reason.

    url_ok checks the start address, every request the route gate sees and the page address after each step.
    addresses_for is the egress proxy's check: the addresses a URL may be reached at ([] = refused); the proxy
    connects only to those. Both must agree on what is allowed. blocked_log, if given, receives every refused
    URL (route gate and proxy), for tests and diagnostics; it is not part of the result.
    """
    if not url_ok(url):
        return _result("not_possible", "blocked_address")
    deadline = clock() + case_timeout
    watchdog = _Watchdog(case_timeout + WATCHDOG_MARGIN, browser)
    watchdog.start()                  # armed before anything that could hang, new_context included
    steps = 0

    def over() -> bool:
        return watchdog.expired.is_set() or clock() >= deadline

    def budget(evidence: str = "", evidence_url: str = "") -> dict:
        return _result("not_possible", "budget", evidence, evidence_url, steps, 2 if steps else 1)

    blocked: list[str] = blocked_log if blocked_log is not None else []   # every refused URL (gate and proxy)
    main_navigation: list[str] = []   # URLs of the main frame's navigation requests, redirect hops included
    main_blocked = threading.Event()  # the route gate refused a main-frame navigation
    # Every request leaves through the egress proxy, which checks also redirect hops and tunnels;
    # the route gate below sees only the first request of each redirect chain, but with the full https URL.
    proxy = EgressProxy(addresses_for, blocked, timeout=nav_timeout, lifetime=case_timeout + WATCHDOG_MARGIN).start()
    context = None
    page = None
    try:
        # Loopback requests (127.0.0.1, localhost, ::1) reach the proxy only because Playwright forces Chromium to
        # proxy loopback for context proxies (its shouldProxyLoopback default). Never pass a "bypass" list here,
        # and server.py refuses to start with PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK set. Guarded by
        # test_redirect_to_loopback_never_leaves_the_proxy and test_subresources_to_loopback_never_reach_it.
        context = browser.new_context(accept_downloads=False, service_workers="block",
                                      proxy={"server": proxy.address})
        watchdog.context = context
        context.add_init_script(NO_WEBRTC_JS)

        def is_main_navigation(request) -> bool:
            try:
                return request.is_navigation_request() and page is not None and request.frame == page.main_frame
            except Exception:
                return False

        def gate(route):
            if url_ok(route.request.url):
                route.continue_()
            else:
                blocked.append(route.request.url)
                if is_main_navigation(route.request):
                    main_blocked.set()
                route.abort("blockedbyclient")

        def navigation_blocked() -> bool:
            refused = {_authority(u) for u in list(blocked)}
            return main_blocked.is_set() or any(_authority(u) in refused for u in main_navigation)

        context.route("**/*", gate)
        page = context.new_page()
        context.on("page", lambda extra: extra.close() if extra != page else None)   # pop-ups and new tabs
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("download", _refuse_download)
        page.on("request", lambda request: main_navigation.append(request.url) if is_main_navigation(request) else None)
        try:
            page.goto(url, timeout=nav_timeout * 1000, wait_until="domcontentloaded")
        except Exception as e:
            if over():
                return budget()
            reason = "blocked_address" if navigation_blocked() else "page_error"
            logger.info("first load failed (%s): %s", reason, type(e).__name__)
            return _result("not_possible", reason)
        _settle(page, nav_timeout)

        history: list[str] = []
        bad_replies = 0
        while True:
            if over():
                return budget()
            limits = [MAX_ELEMENTS, MAX_TEXT, MAX_LABEL, MAX_OPTIONS]
            try:
                raw = page.evaluate(SNAPSHOT_JS, limits)
            except Exception:
                if over():
                    return budget()
                _settle(page, nav_timeout)      # the page was still navigating; one more try
                try:
                    raw = page.evaluate(SNAPSHOT_JS, limits)
                except Exception:
                    if over():
                        return budget()
                    return _result("not_possible", "page_error", steps=steps, stage=2 if steps else 1)
            stage = 2 if steps else 1
            try:
                snap = check_snapshot(raw)       # before anything else uses it
            except SnapshotError:
                logger.info("page returned a malformed snapshot")
                return _result("not_possible", "page_error", steps=steps, stage=stage)
            here = page.url
            if not url_ok(here) and here != "about:blank":
                return _result("not_possible", "blocked_address", steps=steps, stage=stage)
            if has_password(snap["elements"]) or is_login_url(here) or _password_anywhere(page):
                return _result("not_possible", "login", snap["tail"], here, steps, stage)
            if snap["captcha"]:
                return _result("not_possible", "captcha", snap["tail"], here, steps, stage)
            if steps == 0 and looks_unsubscribed(snap["text"]) and not needs_input(snap["elements"]):
                return _result("unsubscribed", "page confirms without interaction", snap["tail"], here, 0, 1)
            if steps >= max_steps or over():
                return _result("not_possible", "budget", snap["tail"], here, steps, 2)

            steps += 1
            prompt = build_prompt(snap["title"], here, snap["text"], snap["elements"], history, bool(address))
            try:
                reply = _ask(llm, prompt, max(deadline - clock(), 0.001))
            except LLMUnavailable as e:
                if over():
                    return budget(snap["tail"], here)
                logger.warning("model unavailable: %s", e)
                return _result("error", "llm_unavailable", steps=steps - 1, stage=2)
            if over():
                return budget(snap["tail"], here)
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
                if over():
                    return budget(snap["tail"], here)
                history.append(f"{action.kind} {action.element} -> failed ({type(e).__name__})")
            _settle(page, nav_timeout)
    except Exception:
        if over():
            return budget()
        raise
    finally:
        watchdog.cancel()
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        proxy.stop()
