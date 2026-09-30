"""
Printer maintenance (nozzle check, cleaning, roller cleaning ...) for AirPrint Bridge - discovered from the driver.

Canon's inkjet driver has no command line: its Maintenance tab sends a small RAW job to the queue - a few XML "cmd"
documents plus BJL lines such as ``@TestPrint=NozzleCheck``.  Those command texts (and the XML wrapper) are stored as
plain strings inside the driver's own DLLs, so this module SCANS THE DRIVER of a queue and builds the actions from what
it finds - a new Canon model needs no code:

  1. read the queue's driver files (CNM*.DLL in the driver folder);
  2. pull out every ``@Cleaning=<level><group>``, ``@TestPrint=<name>`` and ``@RollerCleaning=<name>`` command and the
     StartJob / ModeShift / EndJob XML documents;
  3. label them by rule (level 1 = cleaning, 2 = deep cleaning, 4 = system cleaning ...), hide the risky ones;
  4. send the same bytes the Maintenance tab would send, as a RAW job.

A driver without these strings (Brother, HP, Epson, thermal ...) simply yields no actions; those brands keep maintenance
in the printer's own panel / web page (the web UI links to it when the printer has a network address).
The rules that pick labels / hide actions are the only brand knowledge (RULES below).
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List, Optional

logger = logging.getLogger("airprint_bridge")

_BJL_PREFIX = b"\x1b[K\x02\x00\x00\x1f"
_HDR = b'<?xml version="1.0" encoding="utf-8" ?><cmd xmlns:ivec="http://www.canon.com/ns/cmd/2008/07/common/"'
_DEFAULT_START = (_HDR + b'><ivec:contents><ivec:operation>StartJob</ivec:operation><ivec:param_set servicetype="print">'
                  b'<ivec:jobID>00000001</ivec:jobID><ivec:bidi>0</ivec:bidi></ivec:param_set></ivec:contents></cmd>')
_DEFAULT_MODE = (_HDR + b' xmlns:vcn="http://www.canon.com/ns/cmd/2008/07/canon/"><ivec:contents><ivec:operation>VendorCmd</ivec:operation>'
                 b'<ivec:param_set servicetype="print"><vcn:ijoperation>ModeShift</vcn:ijoperation><vcn:ijmode>1</vcn:ijmode>'
                 b'<ivec:jobID>00000001</ivec:jobID></ivec:param_set></ivec:contents></cmd>')
_DEFAULT_END = (_HDR + b'><ivec:contents><ivec:operation>EndJob</ivec:operation><ivec:param_set servicetype="print">'
                b'<ivec:jobID>00000001</ivec:jobID></ivec:param_set></ivec:contents></cmd>')

# Where one discovered command ends inside the DLL's run-together string pool.
_END = rb"(?=@|\x00|ControlMode|PAPERGAP|<|SetRegi|SetInk|SetSilent|SetTime|DryLevel|AutoPower|\$|BJL|[^\x20-\x7e]|\Z)"
# Any "@Key=Value" command whose key sounds like maintenance (clean, test, check, nozzle, align, calibrate, roller, drum ...)
_VOCAB = r"(?:Clean|Test|Nozzle|Check|Regi|Align|Calib|Roller|Platen|Drum|Purge|Flush|Feed|Diag|SelfTest|Maint|Wipe|Prime|Lubric)"
_TOKEN = re.compile(rb"@(" + _VOCAB.encode() + rb"[A-Za-z]{0,20})=([0-9A-Za-z_]+?)" + _END)
_RISKY = re.compile(r"reset|counter|erase|format|initial|firmware|detect|power|shipment|dry|serial|region|factory|fuse|eeprom|lock", re.I)

_GROUP = {"ALL": "all colours", "K": "black", "CMY": "colour", "C": "cyan", "M": "magenta", "Y": "yellow", "BK": "black"}
_INK = {0: "no ink", 1: "a small amount of ink", 2: "a lot of ink", 3: "a very large amount of ink"}


def _words(text: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])|_", " ", text).strip()


def classify(key: str, value: str) -> Optional[dict]:
    """Label a discovered command by keyword.  Known commands get friendly text; anything else that sounds like
    maintenance is still listed (hidden unless "maintenance_advanced" is set) so nothing the driver offers is lost."""
    ident = re.sub(r"[^a-z0-9]+", "-", f"{key}-{value}".lower()).strip("-")
    text = f"{key} {value}"
    hidden_default = bool(_RISKY.search(text))
    if key == "Cleaning":
        m = re.fullmatch(r"(\d)([A-Z]+)", value)
        if m:
            level, group = int(m.group(1)), _GROUP.get(m.group(2), m.group(2).lower())
            name, cost, desc = {
                1: ("Cleaning", 1, "Regular print-head cleaning."),
                2: ("Deep cleaning", 2, "Only if regular cleaning (twice) did not help."),
                4: ("System cleaning", 3, "Heaviest cleaning; the printer can be damaged if an ink tank is nearly empty."),
            }.get(level, (f"Cleaning level {level}", 2, "Print-head cleaning."))
            return {"id": f"cleaning-{level}-{m.group(2).lower()}", "label": f"{name} - {group}",
                    "desc": f"{desc} Ink group: {group}.", "cost": cost, "hidden": level >= 3}
    known = {("TestPrint", "NozzleCheck"): ("nozzle-check", "Nozzle check", 0, False,
                                            "Prints a test pattern (1 sheet of A4/Letter plain paper) to see whether nozzles are clogged."),
             ("TestPrint", "RegiCheck"): ("alignment-value", "Print head alignment values", 0, False,
                                          "Prints the current head-alignment values (1 sheet plain paper)."),
             ("RollerCleaning", "Roller"): ("roller-cleaning", "Roller cleaning", 0, False,
                                            "Runs the paper-feed rollers 30 times (about 1.5 minutes). Remove paper from the tray first."),
             ("RollerCleaning", "Platen"): ("bottom-plate-cleaning", "Bottom plate cleaning", 0, False,
                                            "Feeds one sheet of A4/Letter plain paper folded in half (crease down) to clean the plate.")}
    if (key, value) in known:
        i, label, cost, hidden, desc = known[(key, value)]
        return {"id": i, "label": label, "desc": desc, "cost": cost, "hidden": hidden}
    # --- generic rules for anything else the driver offers (other models / brands) ---
    lk = text.lower()
    if "drum" in lk:
        label, desc, cost = "Drum " + ("check" if re.search("check|test", lk) else "cleaning"), "Drum unit maintenance.", 0
    elif re.search("clean|purge|flush|prime|wipe", lk):
        label, desc, cost = f"Cleaning ({_words(value)})", "Cleaning command found in the driver.", 2
    elif re.search("regi|align|calib", lk):
        label, desc, cost = f"Alignment / calibration ({_words(value)})", "Alignment step; may need the scanner or user input.", 1
        hidden_default = True
    elif re.search("nozzle|test|check|diag|selftest", lk):
        label, desc, cost = f"Test print ({_words(key)} {_words(value)})", "Prints a test or check page.", 0
    elif re.search("roller|platen|feed", lk):
        label, desc, cost = f"Paper path ({_words(value)})", "Paper-feed cleaning or test.", 0
    else:
        label, desc, cost = f"{_words(key)} {_words(value)}", "Command found in the driver; purpose not recognised.", 1
        hidden_default = True
    return {"id": ident, "label": label, "desc": desc, "cost": cost, "hidden": hidden_default or not re.search("drum|clean|nozzle|test|check|roller|platen|feed", lk)}


_scan_cache: Dict[str, tuple] = {}


def _driver_blob(printer_name: str) -> bytes:
    import win32print
    h = win32print.OpenPrinter(printer_name)
    try:
        drv_name = win32print.GetPrinter(h, 2)["pDriverName"]
    finally:
        win32print.ClosePrinter(h)
    drv = next((d for d in win32print.EnumPrinterDrivers(None, None, 2) if d.get("Name") == drv_name), {})
    files = {drv.get("ConfigFile"), drv.get("DataFile"), drv.get("DriverPath")}
    folder = os.path.dirname(drv.get("DriverPath") or "")
    if folder and os.path.isdir(folder):
        files |= {os.path.join(folder, f) for f in os.listdir(folder) if re.match(r"CNM\w*\.DLL$", f, re.I)}
    blob = b""
    for f in files:
        if f and os.path.isfile(f) and os.path.getsize(f) < 40_000_000:
            with open(f, "rb") as fh:
                blob += fh.read()
    return blob


def _xml_doc(blob: bytes, op: bytes, must: bytes = b"") -> Optional[bytes]:
    """The XML "cmd" document for an operation.  The DLL holds several variants; prefer a finished one (job id already
    written) over a template with a %s placeholder, which is filled with job id 00000001."""
    found = []
    for m in re.finditer(rb'<\?xml version="1\.0" encoding="utf-8" \?><cmd .*?</cmd>', blob, re.S):
        doc = m.group(0)
        if b"<ivec:operation>" + op + b"</ivec:operation>" in doc and must in doc:
            found.append(doc)
    for doc in found:
        if b"%" not in doc:
            return doc
    return re.sub(rb"%0?8?[sd]", b"00000001", found[0]) if found else None


def discover(printer_name: str) -> dict:
    """{'actions': {id: {..., 'bjl': '@Cleaning=1ALL'}}, 'start': bytes, 'mode': bytes, 'end': bytes} for this queue's driver
    (cached an hour).  'actions' is empty when the driver has no such commands."""
    hit = _scan_cache.get(printer_name)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    res = {"actions": {}, "start": _DEFAULT_START, "mode": _DEFAULT_MODE, "end": _DEFAULT_END}
    try:
        blob = _driver_blob(printer_name)
        if b"BJLSTART" in blob and b"ivec:operation" in blob:
            for m in _TOKEN.finditer(blob):
                key, val = m.group(1).decode(), m.group(2).decode()
                a = classify(key, val)
                if a and a["id"] not in res["actions"]:
                    res["actions"][a["id"]] = dict(a, bjl=f"@{key}={val}")
            res["start"] = _xml_doc(blob, b"StartJob", b"<ivec:bidi>0<") or _DEFAULT_START
            res["mode"] = _xml_doc(blob, b"VendorCmd", b"ModeShift") or _DEFAULT_MODE
            res["end"] = _xml_doc(blob, b"EndJob", b'servicetype="print"') or _DEFAULT_END
            logger.info("Maintenance commands found in the driver of %r: %s", printer_name, sorted(res["actions"]))
    except Exception:  # noqa: BLE001
        logger.debug("maintenance: could not scan the driver of %r", printer_name, exc_info=True)
    _scan_cache[printer_name] = (time.time(), res)
    return res


def _all(printer_name: str) -> Dict[str, dict]:
    acts = dict(discover(printer_name)["actions"])
    acts.update(learned(printer_name))                 # recorded ones win over guessed ones of the same id
    return acts


def _learned_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "maintenance-learned.json")


def learned(printer_name: str) -> Dict[str, dict]:
    """Actions recorded with scripts/windows/learn-maintenance.ps1 (any brand): {id: {label, desc, cost, steps:[bytes]}}."""
    import base64
    import json
    try:
        with open(_learned_path(), encoding="utf-8-sig") as fh:          # Windows PowerShell writes a BOM
            data = json.load(fh).get(printer_name, {})
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in data.items():
        try:
            out[k] = {"id": k, "label": v.get("label", k), "desc": v.get("desc", "Recorded from the driver's own Maintenance function."),
                      "cost": int(v.get("cost", 1)), "hidden": False, "learned": True,
                      "steps": [base64.b64decode(b) for b in ([v["steps"]] if isinstance(v["steps"], str) else v["steps"])]}
        except (KeyError, ValueError):
            continue
    return out


def build_job(printer_name: str, bjl_cmd: str, when: Optional[float] = None) -> bytes:
    """The byte stream the driver's Maintenance tab sends for one command."""
    d = discover(printer_name)
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(when or time.time())).encode()
    blocks = [b"BJLSTART\nControlMode=Driver\n$JobType=Mnt\nBJLEND\n",
              b"BJLSTART\nControlMode=Common\nSetTime=" + stamp + b"\nBJLEND\n",
              b"BJLSTART\n" + bjl_cmd.encode("ascii") + b"\nBJLEND\n"]
    return d["start"] + d["mode"] + b"".join(_BJL_PREFIX + b for b in blocks) + d["end"]


def actions(printer_name: str, advanced: bool = False) -> List[dict]:
    """Actions available for this printer: [{id, label, desc, cost}] (risky ones only with advanced=True)."""
    return [{k: a[k] for k in ("id", "label", "desc", "cost")}
            for a in sorted(_all(printer_name).values(), key=lambda a: (a["cost"], a["label"]))
            if advanced or not a["hidden"]]


def run(printer_name: str, action_id: str, advanced: bool = False) -> str:
    """Send one maintenance job to the Windows queue (RAW).  Raises ValueError/RuntimeError when refused."""
    import win32print
    act = _all(printer_name).get(action_id)
    if not act or (act["hidden"] and not advanced):
        raise ValueError("this action is not available for this printer")
    h = win32print.OpenPrinter(printer_name)
    try:
        if win32print.GetPrinter(h, 2).get("cJobs", 0):
            raise RuntimeError("the printer has jobs waiting - wait until they are finished")
        steps = act.get("steps") or [build_job(printer_name, act["bjl"])]
        data = b"".join(steps)
        for step in steps:                              # a recorded action may have been several jobs
            step = re.sub(rb"SetTime=\d{14}", b"SetTime=" + time.strftime("%Y%m%d%H%M%S").encode(), step)   # never set an old clock
            win32print.StartDocPrinter(h, 1, ("Maintenance", None, "RAW"))
            try:
                win32print.StartPagePrinter(h)
                win32print.WritePrinter(h, step)
                win32print.EndPagePrinter(h)
            finally:
                win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)
    logger.info("Maintenance %r (%s) sent to %r (%d bytes)", action_id, act.get("bjl", "recorded"), printer_name, len(data))
    return f"{act['label']} started"
