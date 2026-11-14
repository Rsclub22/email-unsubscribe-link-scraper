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
SUMMARY_KEYS = ("new", "handled", "unsubscribed", "moved", "cleaned", "without_link", "ignored", "error")


def _searchable(key: str) -> bool:
    """Only plain printable ASCII without quote and backslash may go into an IMAP SEARCH command."""
    return bool(key) and all(0x20 <= ord(c) <= 0x7E and c not in '"\\' for c in key)


class AgentUnavailable(Exception):
    """Agent container or model not reachable: the case keeps its attempt."""


class Worker:
    def __init__(self, store, connect, parse, agent, *, inbox="INBOX", done_folder="Erledigt", retention_days=30,
                 max_cases=10, allowed_domains=(), allowed_senders=(), clock=time.time):
        self.store, self.connect, self.parse, self.agent = store, connect, parse, agent
        self.inbox, self.done_folder = inbox, done_folder
        self.retention_days, self.max_cases = retention_days, max_cases
        self.allowed_domains, self.clock = tuple(allowed_domains), clock
        self.allowed_senders = tuple(a.lower() for a in allowed_senders)
        self._lock = threading.Lock()

    def scan(self, mail) -> tuple[dict, int]:
        """Create a case for every mail with unsubscribe links. Returns ({key: uid}, mails without link)."""
        return self._scan(mail)[:2]

    def _scan(self, mail) -> tuple[dict, int, int, int]:
        """Like scan, plus the number of created cases and of ignored mails (no instance state)."""
        _, data = mail.uid("search", None, "ALL")
        uids, without, created, ignored = {}, 0, 0, 0
        for uid in data[0].split():
            try:
                _, fetched = mail.uid("fetch", uid, "(BODY.PEEK[])")
                message = email.message_from_bytes(fetched[0][1])
                subject, links, text = self.parse(message)
                if not links:
                    without += 1
                    continue
                forwarder = parseaddr(message.get("From", ""))[1]
                if (self.allowed_senders and forwarder.lower() not in self.allowed_senders) or (
                        not self.allowed_senders and self.agent is not None):   # fail closed
                    ignored += 1
                    continue
                key = case_key(message)
                recipient = original_recipient(text, forwarder, self.allowed_domains)
            except Exception as e:   # one broken mail must not stop the run
                logger.warning("mail %s skipped: %s", uid, type(e).__name__)
                continue
            uids[key] = uid
            if self.store.add(key, subject, links, recipient):
                created += 1
        return uids, without, created, ignored

    def _handle(self, case: dict) -> str:
        """Run one case through the agent. Returns the final state; raises AgentUnavailable."""
        self.store.claim(case["key"])
        result = {"result": "unclear", "reason": "no link"}
        try:
            for link in case["links"][:MAX_LINKS]:
                result = self.agent(link, case["address"])
                if result["result"] not in FINAL_RESULTS:
                    raise ValueError("malformed agent result")
                if result["result"] == "error":
                    raise AgentUnavailable(result.get("reason", "agent error"))
                if result["result"] == "unsubscribed" or result.get("reason") == "login":
                    break
        except AgentUnavailable:
            self.store.release(case["key"])
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
        summary = {"new": 0, "handled": 0, "unsubscribed": 0, "moved": 0, "cleaned": 0, "without_link": 0, "ignored": 0, "error": ""}
        if not self._lock.acquire(blocking=False):
            summary["error"] = "already running"
            return summary
        try:
            if self.store.meta("paused") == "1":
                summary["error"] = "paused"
                return self._note(summary)
            mail = self.connect()
            try:
                uids, summary["without_link"], summary["new"], summary["ignored"] = self._scan(mail)
                if self.agent is not None:
                    for case in self.store.pending(10_000):
                        if summary["handled"] >= self.max_cases:
                            break
                        if case["key"] not in uids:   # only cases this scan vouched for (mail there, sender allowed)
                            continue
                        try:
                            state = self._handle(case)
                        except AgentUnavailable as e:
                            summary["error"] = str(e)
                            break
                        summary["handled"] += 1
                        summary["unsubscribed"] += state == "unsubscribed"
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
