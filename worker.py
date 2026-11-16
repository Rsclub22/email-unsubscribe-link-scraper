"""Scheduler of the unsubscribe agent: find cases in the inbox, hand them to the agent, tidy up."""

import email
import json
import logging
import threading
import time
from email.utils import parseaddr

from cases import case_key
from forwarded import original_recipient

logger = logging.getLogger(__name__)

MAX_LINKS = 2
FINAL_RESULTS = ("unsubscribed", "not_possible", "unclear", "error")
SUMMARY_KEYS = ("new", "handled", "unsubscribed", "moved", "cleaned", "without_link", "ignored", "error", "deferred",
                "gone")
MODEL_UNAVAILABLE = "model unavailable for this case"


def _searchable(key: str) -> bool:
    """Only plain printable ASCII without quote and backslash may go into an IMAP SEARCH command."""
    return bool(key) and all(0x20 <= ord(c) <= 0x7E and c not in '"\\' for c in key)


class AgentUnavailable(Exception):
    """Agent container not reachable (transport failure): the case keeps its attempt and the run stops."""


class ScanFailed(Exception):
    """The inbox SEARCH did not answer OK: nothing about the inbox is known."""


class _Deferred(Exception):
    """The agent answered error (model unavailable for this page): the case waits, the run goes on."""

    def __init__(self, reason: str, final: bool):
        super().__init__(reason)
        self.final = final


class Worker:
    def __init__(self, store, connect, parse, agent, *, inbox="INBOX", done_folder="Erledigt", retention_days=30,
                 max_cases=10, allowed_domains=(), allowed_senders=(), max_deferrals=12, clock=time.time):
        self.store, self.connect, self.parse, self.agent = store, connect, parse, agent
        self.inbox, self.done_folder = inbox, done_folder
        self.retention_days, self.max_cases, self.max_deferrals = retention_days, max_cases, max_deferrals
        self.allowed_domains, self.clock = tuple(allowed_domains), clock
        self.allowed_senders = tuple(a.lower() for a in allowed_senders)
        self._lock = threading.Lock()

    def scan(self, mail) -> tuple[dict, int]:
        """Create a case for every mail with unsubscribe links. Returns ({key: uid}, mails without link)."""
        return self._scan(mail)[:2]

    def _scan(self, mail) -> tuple[dict, int, int, int, set | None]:
        """Like scan, plus the number of created cases, of ignored mails and the keys of all mails in the inbox
        (None if a mail could not be read, so its key is unknown). No instance state. Raises ScanFailed."""
        status, data = mail.uid("search", None, "ALL")
        if status != "OK":
            raise ScanFailed("inbox scan failed")
        uids, without, created, ignored, present, complete = {}, 0, 0, 0, set(), True
        for uid in data[0].split():
            key = None
            try:
                _, fetched = mail.uid("fetch", uid, "(BODY.PEEK[])")
                message = email.message_from_bytes(fetched[0][1])
                key = case_key(message)
                present.add(key)
                subject, links, text = self.parse(message)
                if not links:
                    without += 1
                    continue
                forwarder = parseaddr(message.get("From", ""))[1]
                if (self.allowed_senders and forwarder.lower() not in self.allowed_senders) or (
                        not self.allowed_senders and self.agent is not None):   # fail closed
                    ignored += 1
                    continue
                recipient = original_recipient(text, forwarder, self.allowed_domains)
            except Exception as e:   # one broken mail must not stop the run
                logger.warning("mail %s skipped: %s", uid, type(e).__name__)
                if key is None:
                    complete = False   # not even its key is known
                continue
            uids[key] = uid
            if self.store.add(key, subject, links, recipient):
                created += 1
        return uids, without, created, ignored, present if complete else None

    def _tidy(self, present: set, since: float) -> int:
        """Cases whose mail is no longer in the inbox: nothing left to do or to show, except an unmoved success,
        which now counts as moved so that it expires. Running cases, and cases created after the scan started (by
        the page's own scan, for a mail this scan did not see), are left alone."""
        gone = 0
        for case in self.store.all():
            if case["key"] in present or case["created_at"] >= since:
                continue
            if case["state"] in ("new", "not_possible", "unclear"):
                self.store.delete(case["key"])
            elif case["state"] == "unsubscribed" and not case["moved"]:
                self.store.mark_moved(case["key"])
            else:
                continue
            gone += 1
        return gone

    def _handle(self, case: dict) -> str:
        """Run one case through the agent. Returns the final state; raises AgentUnavailable or _Deferred."""
        self.store.claim(case["key"])
        result = {"result": "unclear", "reason": "no link"}
        try:
            for link in case["links"][:MAX_LINKS]:
                result = self.agent(link, case["address"])
                if result["result"] not in FINAL_RESULTS:
                    raise ValueError("malformed agent result")
                if result["result"] == "error":
                    raise _Deferred(str(result.get("reason") or "agent error"), False)
                if result["result"] == "unsubscribed" or result.get("reason") == "login":
                    break
        except AgentUnavailable:
            self.store.release(case["key"])
            raise
        except _Deferred as e:
            if self.store.defer(case["key"]) >= self.max_deferrals:
                self.store.finish(case["key"], "unclear", MODEL_UNAVAILABLE)
                e.final = True
            raise
        except Exception as e:   # agent bug or malformed result: the attempt counts, the run goes on
            logger.warning("agent failed on a case: %s", type(e).__name__)
            self.store.finish(case["key"], "unclear", "agent error")
            return "unclear"
        self.store.finish(case["key"], result["result"], result.get("reason", ""), result.get("evidence", ""),
                          result.get("evidence_url", ""))
        return result["result"]

    def _move(self, mail, uids: dict) -> int:
        moved = 0
        todo = [c for c in self.store.to_move() if c["key"] in uids]
        if not todo:
            return 0
        mail.create(self.done_folder)   # answers NO if it exists already
        for case in todo:
            uid = uids[case["key"]]
            status, _ = mail.uid("copy", uid, self.done_folder)
            if status != "OK":
                logger.warning("could not move a mail to %s", self.done_folder)
                continue
            status, _ = mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
            if status != "OK":
                logger.warning("could not flag a copied mail as deleted")
                continue
            self.store.mark_moved(case["key"])
            moved += 1
        mail.expunge()
        return moved

    def _cleanup(self, mail) -> int:
        expired = self.store.expired(self.retention_days, now=self.clock())
        if not expired:
            return 0
        status, _ = mail.select(self.done_folder)
        if status != "OK":
            return 0
        try:
            for case in expired:
                key = case["key"]
                if not key.startswith("sha256:") and _searchable(key):   # others cannot be found (or not safely)
                    _, data = mail.uid("search", None, "HEADER", "Message-ID", '"' + key + '"')
                    for uid in data[0].split():
                        mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
                self.store.delete(case["key"])
            mail.expunge()
        finally:
            mail.select(self.inbox)
        return len(expired)

    def run_once(self) -> dict:
        summary = {k: 0 for k in SUMMARY_KEYS}
        summary["error"] = ""
        if not self._lock.acquire(blocking=False):
            summary["error"] = "already running"
            return summary
        try:
            if self.store.meta("paused") == "1":
                summary["error"] = "paused"
                return self._note(summary)
            mail = self.connect()
            try:
                started = time.time()   # the store's clock, not self.clock
                try:
                    uids, summary["without_link"], summary["new"], summary["ignored"], present = self._scan(mail)
                except ScanFailed as e:   # nothing is known about the inbox: change nothing
                    summary["error"] = str(e)
                    return self._note(summary)
                if present is not None:   # only a complete scan may tidy up
                    summary["gone"] = self._tidy(present, started)
                if self.agent is not None:
                    deferral = ""
                    for case in self.store.pending(10_000):
                        if summary["handled"] + summary["deferred"] >= self.max_cases:   # every agent call counts
                            break
                        if case["key"] not in uids:   # only cases this scan vouched for (mail there, sender allowed)
                            continue
                        if self.store.meta("paused") == "1":   # "Pause agent" also stops a run in progress
                            summary["error"] = "paused"
                            break
                        try:
                            state = self._handle(case)
                        except AgentUnavailable as e:
                            summary["error"] = str(e)
                            break
                        except _Deferred as e:
                            if e.final:       # deferred too often: finished as unclear
                                summary["handled"] += 1
                            else:
                                summary["deferred"] += 1
                                deferral = str(e)
                            continue
                        summary["handled"] += 1
                        summary["unsubscribed"] += state == "unsubscribed"
                    if deferral and not summary["handled"] and not summary["error"]:
                        summary["error"] = deferral   # no case could be handled at all
                summary["moved"] = self._move(mail, uids)
                summary["cleaned"] = self._cleanup(mail)
            finally:
                mail.logout()
            return self._note(summary)
        finally:
            self._lock.release()

    def _note(self, summary: dict) -> dict:
        self.store.set_meta("last_run", json.dumps(summary))
        self.store.set_meta("last_run_at", str(int(self.clock())))
        return summary


def _tick(worker: Worker) -> None:
    """One scheduled run. Never raises; notes only the exception type (messages can contain mail content)."""
    try:
        worker.run_once()
    except Exception as e:
        name = type(e).__name__
        logger.warning("scheduled run failed: %s", name)
        try:
            summary = {k: 0 for k in SUMMARY_KEYS}
            summary["error"] = name
            worker.store.set_meta("last_run", json.dumps(summary))
            worker.store.set_meta("last_run_at", str(int(worker.clock())))
        except Exception as e2:
            logger.warning("could not note the failed run: %s", type(e2).__name__)


def start_scheduler(worker: Worker, interval_seconds: int) -> threading.Thread:
    """Background thread: one run every interval. Errors (mailbox not connected, IMAP down) are noted, not raised."""

    def loop():
        while True:
            _tick(worker)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, name="unsubscribe-scheduler", daemon=True)
    thread.start()
    return thread
