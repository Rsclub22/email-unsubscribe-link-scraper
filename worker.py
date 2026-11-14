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
        _, data = mail.uid("search", None, "ALL")
        uids, without, self._created, self._ignored = {}, 0, 0, 0
        for uid in data[0].split():
            try:
                _, fetched = mail.uid("fetch", uid, "(BODY.PEEK[])")
                message = email.message_from_bytes(fetched[0][1])
                subject, links, text = self.parse(message)
            except Exception as e:   # one broken mail must not stop the run
                logger.warning("mail %s skipped: %s", uid, type(e).__name__)
                continue
            if not links:
                without += 1
                continue
            forwarder = parseaddr(message.get("From", ""))[1]
            sender = forwarder.lower()
            if (self.allowed_senders and sender not in self.allowed_senders) or (
                    not self.allowed_senders and self.agent is not None):   # fail closed
                self._ignored += 1
                continue
            key = case_key(message)
            uids[key] = uid
            if self.store.add(key, subject, links, original_recipient(text, forwarder, self.allowed_domains)):
                self._created += 1
        return uids, without

    def _handle(self, case: dict) -> str:
        """Run one case through the agent. Returns the final state; raises AgentUnavailable."""
        self.store.claim(case["key"])
        result = {"result": "unclear", "reason": "no link"}
        try:
            for link in case["links"][:MAX_LINKS]:
                result = self.agent(link, case["address"])
                if result["result"] == "error":
                    raise AgentUnavailable(result.get("reason", "agent error"))
                if result["result"] == "unsubscribed" or result.get("reason") == "login":
                    break
        except AgentUnavailable:
            self.store.release(case["key"])
            raise
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
            mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
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
                if not case["key"].startswith("sha256:"):   # mails without Message-ID cannot be found again
                    _, data = mail.uid("search", None, "HEADER", "Message-ID", '"' + case["key"].replace('"', "") + '"')
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
                uids, summary["without_link"] = self.scan(mail)
                summary["new"], summary["ignored"] = self._created, self._ignored
                if self.agent is not None:
                    for case in self.store.pending(self.max_cases):
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


def start_scheduler(worker: Worker, interval_seconds: int) -> threading.Thread:
    """Background thread: one run every interval. Errors (mailbox not connected, IMAP down) are noted, not raised."""

    def loop():
        while True:
            try:
                worker.run_once()
            except Exception as e:
                logger.warning("scheduled run failed: %s: %s", type(e).__name__, e)
                worker.store.set_meta("last_run", json.dumps({"error": f"{type(e).__name__}: {e}"[:200]}))
                worker.store.set_meta("last_run_at", str(int(worker.clock())))
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, name="unsubscribe-scheduler", daemon=True)
    thread.start()
    return thread
