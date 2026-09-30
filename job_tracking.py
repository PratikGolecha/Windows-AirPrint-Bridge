"""
Print-job tracking and real printer status for AirPrint Bridge.

The bridge hands every document to the Windows print spooler.  This module lets it tell phones the truth:
  * printer_status()  - idle / printing / stopped, with the reason (out of paper, jam, offline, door open, toner ...)
                        and how many jobs are waiting, read from the Windows queue;
  * JobTracker        - IPP job ids and their state (pending -> processing -> completed / stopped / aborted /
                        canceled) for Get-Job-Attributes, Get-Jobs and Cancel-Job;
  * wait_spooler_job()- follows one job inside the Windows spooler until it has really left the queue.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("airprint_bridge")

# IPP job-state values
JOB_PENDING, JOB_PROCESSING, JOB_STOPPED, JOB_CANCELED, JOB_ABORTED, JOB_COMPLETED = 3, 5, 6, 7, 8, 9
# IPP printer-state values
PRINTER_IDLE, PRINTER_PROCESSING, PRINTER_STOPPED = 3, 4, 5

# Windows PRINTER_STATUS_* bits -> IPP printer-state-reasons keyword
_PRINTER_FLAGS = [
    (0x00000001, "paused", True),                       # PAUSED
    (0x00000002, "other-error", True),                  # ERROR
    (0x00000008, "media-jam-error", True),              # PAPER_JAM
    (0x00000010, "media-empty-error", True),            # PAPER_OUT
    (0x00000040, "media-needed-error", True),           # PAPER_PROBLEM
    (0x00000080, "offline-report", True),               # OFFLINE
    (0x00001000, "offline-report", True),               # NOT_AVAILABLE
    (0x00040000, "marker-supply-empty-error", True),    # NO_TONER
    (0x00020000, "marker-supply-low-warning", False),   # TONER_LOW
    (0x00100000, "other-warning", False),               # USER_INTERVENTION
    (0x00400000, "door-open-error", True),              # DOOR_OPEN
    (0x00800000, "output-area-full-error", True),       # OUTPUT_BIN_FULL
    (0x00080000, "other-error", True),                  # OUT_OF_MEMORY
]
_PRINTER_STATUS_PRINTING = 0x00000400
_PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x00000400

# Windows JOB_STATUS_* bits
_JOB_PAUSED, _JOB_ERROR, _JOB_DELETING, _JOB_SPOOLING, _JOB_PRINTING = 0x1, 0x2, 0x4, 0x8, 0x10
_JOB_OFFLINE, _JOB_PAPEROUT, _JOB_PRINTED, _JOB_DELETED = 0x20, 0x40, 0x80, 0x100
_JOB_BLOCKED, _JOB_USER_INT, _JOB_COMPLETE = 0x200, 0x400, 0x1000

_status_cache: Dict[str, tuple] = {}


def printer_status(printer_name: str, max_age: float = 3.0) -> dict:
    """{'state': 3|4|5, 'reasons': [...], 'queued': n} read from the Windows queue (cached a few seconds)."""
    hit = _status_cache.get(printer_name)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    result = {"state": PRINTER_IDLE, "reasons": ["none"], "queued": 0}
    try:
        import win32print
        h = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(h, 2)
        finally:
            win32print.ClosePrinter(h)
        status = int(info.get("Status", 0))
        reasons: List[str] = []
        stopped = False
        for bit, keyword, stops in _PRINTER_FLAGS:
            if status & bit and keyword not in reasons:
                reasons.append(keyword)
                stopped = stopped or stops
        if int(info.get("Attributes", 0)) & _PRINTER_ATTRIBUTE_WORK_OFFLINE and "offline-report" not in reasons:
            reasons.append("offline-report")           # "Use printer offline" ticked
            stopped = True
        state = PRINTER_STOPPED if stopped else (PRINTER_PROCESSING if status & _PRINTER_STATUS_PRINTING else PRINTER_IDLE)
        result = {"state": state, "reasons": reasons or ["none"], "queued": int(info.get("cJobs", 0))}
    except Exception:  # noqa: BLE001 - never let a status query break a request
        logger.debug("printer_status failed for %r", printer_name, exc_info=True)
    _status_cache[printer_name] = (time.time(), result)
    return result


class JobTracker:
    """IPP job ids and states, kept in memory (the last ``keep`` jobs)."""

    def __init__(self, keep: int = 200) -> None:
        self._jobs: Dict[int, dict] = {}
        self._lock = threading.Lock()
        self._next = 1
        self._keep = keep

    def create(self, printer: str, name: str = "", user: str = "") -> int:
        with self._lock:
            jid = self._next
            self._next += 1
            self._jobs[jid] = {"id": jid, "printer": printer, "name": name, "user": user, "state": JOB_PENDING,
                               "reasons": ["job-incoming"], "created": time.time(), "finished": None,
                               "cancel": threading.Event(), "spool_id": None}
            for old in sorted(self._jobs)[:-self._keep]:
                self._jobs.pop(old, None)
        return jid

    def get(self, jid: int) -> Optional[dict]:
        return self._jobs.get(jid)

    def set(self, jid: int, state: int, reasons: Optional[List[str]] = None) -> None:
        job = self._jobs.get(jid)
        if not job:
            return
        job["state"] = state
        job["reasons"] = reasons or ["none"]
        if state >= JOB_CANCELED and not job["finished"]:
            job["finished"] = time.time()

    def list(self, printer: str, completed: Optional[bool] = None) -> List[dict]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j["printer"] == printer]
        if completed is True:
            jobs = [j for j in jobs if j["state"] >= JOB_CANCELED]
        elif completed is False:
            jobs = [j for j in jobs if j["state"] < JOB_CANCELED]
        return sorted(jobs, key=lambda j: j["id"])

    def cancel(self, jid: int) -> bool:
        job = self._jobs.get(jid)
        if not job or job["state"] >= JOB_CANCELED:
            return False
        job["cancel"].set()
        self.set(jid, JOB_CANCELED, ["job-canceled-by-user"])
        return True


def _find_spooler_job(printer_name: str, doc_name: str) -> Optional[dict]:
    import win32print
    h = win32print.OpenPrinter(printer_name)
    try:
        for j in win32print.EnumJobs(h, 0, 200, 1):
            if j.get("pDocument") == doc_name:
                return j
    finally:
        win32print.ClosePrinter(h)
    return None


def delete_spooler_job(printer_name: str, doc_name: str) -> bool:
    """Remove a job from the Windows queue (used by Cancel-Job)."""
    try:
        import win32print
        job = _find_spooler_job(printer_name, doc_name)
        if not job:
            return False
        h = win32print.OpenPrinter(printer_name)
        try:
            win32print.SetJob(h, job["JobId"], 0, None, win32print.JOB_CONTROL_DELETE)
        finally:
            win32print.ClosePrinter(h)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("could not delete spooler job %r", doc_name)
        return False


def wait_spooler_job(tracker: JobTracker, jid: int, printer_name: str, doc_name: str,
                     timeout: float = 900.0, log: Callable[[str], None] = logger.info) -> str:
    """Follow the job through the Windows spooler and keep the IPP job state in step with it.
    Returns 'completed', 'aborted', 'canceled' or 'timeout'."""
    job = tracker.get(jid)
    started = time.time()
    seen = False
    last = None
    while time.time() - started < timeout:
        if job and job["cancel"].is_set():
            delete_spooler_job(printer_name, doc_name)
            return "canceled"
        try:
            sj = _find_spooler_job(printer_name, doc_name)
        except Exception:  # noqa: BLE001
            logger.debug("EnumJobs failed", exc_info=True)
            sj = None
        if sj is not None:
            seen = True
            if job:
                job["spool_id"] = sj["JobId"]
            st = int(sj.get("Status", 0))
            if st & (_JOB_DELETING | _JOB_DELETED):
                tracker.set(jid, JOB_ABORTED, ["aborted-by-system"])
                return "aborted"
            if st & (_JOB_ERROR | _JOB_BLOCKED | _JOB_USER_INT | _JOB_OFFLINE | _JOB_PAPEROUT):
                reasons = ["printer-stopped"]
                if st & _JOB_PAPEROUT:
                    reasons.append("media-empty")
                if st & _JOB_OFFLINE:
                    reasons.append("offline-report")
                new = (JOB_STOPPED, tuple(reasons))
            elif st & _JOB_PAUSED:
                new = (JOB_STOPPED, ("job-hold-until-specified",))
            else:
                new = (JOB_PROCESSING, ("job-printing",))
            if new != last:
                tracker.set(jid, new[0], list(new[1]))
                last = new
        elif seen or time.time() - started > 10:
            tracker.set(jid, JOB_COMPLETED, ["job-completed-successfully"])
            return "completed"
        time.sleep(1.0 if time.time() - started < 30 else 2.0)
    tracker.set(jid, JOB_COMPLETED, ["job-completed-with-warnings"])
    return "timeout"
