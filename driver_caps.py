"""
Driver capabilities for AirPrint Bridge, read from the Windows Print Schema.

Every modern Windows print driver (Canon, Brother, HP, Epson ...) describes what it can do in a PrintCapabilities
document (media types, borderless, quality, colour, sizes, resolutions ...).  This module
  * reads and caches that document per printer (PowerShell + System.Printing),
  * turns it into the lists the bridge advertises to phones (media types, quality levels, borderless, sizes),
  * builds a PrintTicket from the job's choices, lets Windows validate it and convert it to a DEVMODE, so the
    driver receives exactly what its own print dialog would have produced.

Only the standard "psk:" features are used (PageMediaType, PageOutputQuality, PageBorderless, PageMediaSize,
PageOutputColor, PageResolution, PageOrientation), so the same code works for every vendor.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("airprint_bridge")

_PSF = "{http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework}"
_HERE = Path(__file__).resolve().parent
_PS_FILE = _HERE / "_printschema.ps1"
_CACHE_DIR = _HERE / "driver-caps"
_CACHE_MAX_AGE = 24 * 3600

_PS_SCRIPT = r'''param([string]$Mode,[string]$Queue,[string]$File)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Printing, ReachFramework
$q = (New-Object System.Printing.LocalPrintServer).GetPrintQueue($Queue)
if ($Mode -eq 'caps') {
    $s = $q.GetPrintCapabilitiesAsXml()
    $r = New-Object IO.StreamReader($s, [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($File, $r.ReadToEnd(), (New-Object Text.UTF8Encoding($false)))
} elseif ($Mode -eq 'devmode') {
    $ms = New-Object IO.MemoryStream(,[IO.File]::ReadAllBytes($File))
    $delta = New-Object System.Printing.PrintTicket($ms)
    $res = $q.MergeAndValidatePrintTicket($q.UserPrintTicket, $delta)
    $conv = New-Object System.Printing.Interop.PrintTicketConverter($Queue, $q.ClientPrintSchemaVersion)
    $dm = $conv.ConvertPrintTicketToDevMode($res.ValidatedPrintTicket, [System.Printing.Interop.BaseDevModeType]::UserDefault)
    [IO.File]::WriteAllBytes($File + '.dm', $dm)
    [IO.File]::WriteAllText($File + '.status', [string]$res.ConflictStatus)
}
'''

# option display name / keyword  ->  IPP media-type keyword (first match wins, in this order)
_TYPE_RULES = [
    (r"envelope", "envelope"),
    (r"matte", "photographic-matte"),
    (r"semi.?gloss|silky|lust(er|re)|satin|pearl", "photographic-semi-gloss"),
    (r"glossy|photo", "photographic-glossy"),
    (r"transparen|ohp", "transparency"),
    (r"label", "labels"),
    (r"post.?card|hagaki|card|thick", "cardstock"),
    (r"plain|stationery|normal paper|standard paper|regular", "stationery"),
]

_cache: Dict[str, Tuple[float, "Caps"]] = {}
_devmode_cache: Dict[Tuple[str, str], bytes] = {}
_lock = threading.Lock()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "x"


def _run_ps(mode: str, queue: str, file: str, timeout: int = 60) -> None:
    if not _PS_FILE.exists() or _PS_FILE.read_text(encoding="utf-8", errors="ignore") != _PS_SCRIPT:
        _PS_FILE.write_text(_PS_SCRIPT, encoding="utf-8")
    flags = 0x08000000  # CREATE_NO_WINDOW
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(_PS_FILE), "-Mode", mode, "-Queue", queue, "-File", file],
        capture_output=True, timeout=timeout, creationflags=flags)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).decode("utf-8", "replace").strip()[:500])


class Caps:
    """Parsed PrintCapabilities of one printer."""

    def __init__(self, printer: str, xml_text: str) -> None:
        self.printer = printer
        self.xml_text = xml_text
        root = ET.fromstring(xml_text.encode("utf-8"))
        m = re.search(r"<(?!\?)[^>]*>", xml_text)                  # the root element (skips the <?xml ...?> line)
        head = m.group(0) if m else ""
        self.ns_decls = dict(re.findall(r'xmlns:(\w+)="([^"]+)"', head))
        self.features: Dict[str, dict] = {}
        for f in root.findall(_PSF + "Feature"):
            opts = []
            for o in f.findall(_PSF + "Option"):
                props = {}
                display = ""
                for p in o.findall(_PSF + "Property"):
                    if p.get("name") == "psk:DisplayName":
                        v = p.find(_PSF + "Value")
                        display = (v.text or "") if v is not None else ""
                for sp in o.findall(_PSF + "ScoredProperty"):
                    v = sp.find(_PSF + "Value")
                    if v is not None:
                        props[sp.get("name")] = (v.text or "").strip()
                opts.append({"name": o.get("name"), "display": display, "props": props})
            self.features[f.get("name")] = {"options": opts}

    # ---- generic ----------------------------------------------------------
    def options(self, feature: str) -> List[dict]:
        return self.features.get(feature, {}).get("options", [])

    def has(self, feature: str) -> bool:
        return bool(self.options(feature))

    # ---- media types ------------------------------------------------------
    def media_types(self) -> List[dict]:
        """[{keyword, option, display, borderless}] - IPP media-type keyword for every driver option."""
        out, used = [], set()
        for o in self.options("psk:PageMediaType"):
            text = f"{o['display']} {o['name']}".lower()
            kw = next((k for pat, k in _TYPE_RULES if re.search(pat, text)), None)
            if kw is None or kw in used:
                kw = _slug(o["display"] or o["name"].split(":")[-1])
            base, n = kw, 2
            while kw in used:
                kw, n = f"{base}-{n}", n + 1
            used.add(kw)
            out.append({"keyword": kw, "option": o["name"], "display": o["display"],
                        "borderless": o["props"].get("ns0000:BorderlessPrinting", "Supported") != "None"})
        return out

    def media_type_option(self, keyword: str) -> Optional[str]:
        for m in self.media_types():
            if m["keyword"] == keyword:
                return m["option"]
        return None

    # ---- quality ----------------------------------------------------------
    def quality_options(self) -> Dict[int, str]:
        """IPP print-quality (3 draft, 4 normal, 5 high) -> driver option name."""
        found: Dict[int, str] = {}
        for o in self.options("psk:PageOutputQuality"):
            n = o["name"].lower()
            if n.endswith(":draft"):
                found.setdefault(3, o["name"])
            elif n.endswith(":normal"):
                found.setdefault(4, o["name"])
            elif n.endswith(":high"):
                found.setdefault(5, o["name"])
            elif n.endswith(":automatic"):
                found.setdefault(0, o["name"])
        if 4 not in found and 0 in found:
            found[4] = found[0]
        found.pop(0, None)
        return found

    # ---- two-sided printing / paper trays ---------------------------------
    def duplex_options(self) -> Dict[str, str]:
        """IPP sides keyword -> driver option, only when the printer turns the page itself (not 'manual' duplex)."""
        manual = any("duplexmode" in f.lower() and all("manual" in o["name"].lower() for o in v["options"])
                     for f, v in self.features.items())
        out: Dict[str, str] = {}
        if manual:
            return out
        for o in self.options("psk:JobDuplexAllDocumentsContiguously"):
            n = o["name"].lower()
            if n.endswith(":onesided"):
                out["one-sided"] = o["name"]
            elif n.endswith("twosidedlongedge"):
                out["two-sided-long-edge"] = o["name"]
            elif n.endswith("twosidedshortedge"):
                out["two-sided-short-edge"] = o["name"]
        return out if len(out) > 1 else {}

    def input_bins(self) -> List[dict]:
        """[{keyword, option, display}] for paper trays (IPP media-source keywords where they exist)."""
        out, used = [], set()
        for o in self.options("psk:JobInputBin"):
            text = f"{o['name']} {o['display']}".lower()
            m = re.search(r"tray\W*(\d+)", text)
            if "auto" in text:
                kw = "auto"
            elif "manual" in text:
                kw = "manual"
            elif re.search(r"by.?pass|multi|mp\b", text):
                kw = "by-pass-tray"
            elif "envelope" in text:
                kw = "envelope"
            elif "upper" in text or "top" in text:
                kw = "top"
            elif "lower" in text or "bottom" in text:
                kw = "bottom"
            elif m:
                kw = f"tray-{m.group(1)}"
            else:
                kw = _slug(o["display"] or o["name"].split(":")[-1])
            base, n = kw, 2
            while kw in used:
                kw, n = f"{base}-{n}", n + 1
            used.add(kw)
            out.append({"keyword": kw, "option": o["name"], "display": o["display"]})
        return out

    def input_bin_option(self, keyword: str) -> Optional[str]:
        return next((b["option"] for b in self.input_bins() if b["keyword"] == keyword), None)

    # ---- borderless / colour / sizes / resolutions ------------------------
    def borderless_supported(self) -> bool:
        return any(o["name"].lower().endswith(":borderless") for o in self.options("psk:PageBorderless"))

    def color_option(self, mono: bool) -> Optional[str]:
        for o in self.options("psk:PageOutputColor"):
            n = o["name"].lower()
            if mono and re.search(r"mono|gray|grey|black", n):
                return o["name"]
            if not mono and n.endswith(":color"):
                return o["name"]
        return None

    def sizes(self) -> List[Tuple[str, float, float]]:
        out = []
        for o in self.options("psk:PageMediaSize"):
            try:
                w = int(o["props"]["psk:MediaSizeWidth"]) / 1000.0
                h = int(o["props"]["psk:MediaSizeHeight"]) / 1000.0
            except (KeyError, ValueError):
                continue
            if w > 0 and h > 0:
                out.append((o["name"], w, h))
        return out

    def size_option(self, w_mm: float, h_mm: float) -> Optional[str]:
        for name, w, h in self.sizes():
            if abs(w - w_mm) < 2.0 and abs(h - h_mm) < 2.0:
                return name
        for name, w, h in self.sizes():                       # rotated
            if abs(w - h_mm) < 2.0 and abs(h - w_mm) < 2.0:
                return name
        return None

    def resolutions(self) -> List[Tuple[int, int]]:
        out = []
        for o in self.options("psk:PageResolution"):
            try:
                x = int(o["props"].get("psk:ResolutionX") or re.search(r"(\d+)x\d+", o["name"]).group(1))
                y = int(o["props"].get("psk:ResolutionY") or re.search(r"\d+x(\d+)", o["name"]).group(1))
            except (AttributeError, ValueError):
                continue
            if (x, y) not in out:
                out.append((x, y))
        return out

    def summary(self) -> dict:
        return {
            "media_types": [{"keyword": m["keyword"], "name": m["display"], "borderless": m["borderless"]}
                            for m in self.media_types()],
            "quality": sorted(self.quality_options()),
            "borderless": self.borderless_supported(),
            "duplex": sorted(self.duplex_options()),
            "trays": [b["keyword"] for b in self.input_bins()],
            "color": bool(self.color_option(False)),
            "mono": bool(self.color_option(True)),
            "sizes": len(self.sizes()),
            "resolutions": self.resolutions(),
            "features": sorted(self.features),
        }

    # ---- PrintTicket -> DEVMODE -------------------------------------------
    def devmode(self, choices: Dict[str, str]) -> Optional[bytes]:
        """DEVMODE bytes for {feature: option} choices (validated by the driver); None if it fails."""
        if not choices:
            return None
        key = (self.printer, json.dumps(choices, sort_keys=True))
        with _lock:
            if key in _devmode_cache:
                return _devmode_cache[key]
        decl = " ".join(f'xmlns:{p}="{u}"' for p, u in self.ns_decls.items())
        body = "".join(f'<psf:Feature name="{f}"><psf:Option name="{o}"/></psf:Feature>' for f, o in choices.items())
        ticket = f'<?xml version="1.0" encoding="UTF-8"?><psf:PrintTicket {decl} version="1">{body}</psf:PrintTicket>'
        fd, path = tempfile.mkstemp(prefix="pt_", suffix=".xml")
        os.close(fd)
        try:
            Path(path).write_text(ticket, encoding="utf-8")
            _run_ps("devmode", self.printer, path)
            data = Path(path + ".dm").read_bytes()
            try:
                logger.info("PrintTicket %s -> DEVMODE %d bytes (%s)", choices, len(data),
                            Path(path + ".status").read_text().strip())
            except OSError:
                pass
        except Exception:  # noqa: BLE001
            logger.exception("PrintTicket conversion failed for %r", choices)
            return None
        finally:
            for p in (path, path + ".dm", path + ".status"):
                try:
                    os.remove(p)
                except OSError:
                    pass
        with _lock:
            _devmode_cache[key] = data
        return data


def load(printer: str, refresh: bool = False) -> Optional[Caps]:
    """Capabilities of *printer* (memory + disk cache, refreshed daily).  None if the driver has no Print Schema."""
    with _lock:
        hit = _cache.get(printer)
    if hit and not refresh and time.time() - hit[0] < _CACHE_MAX_AGE:
        return hit[1]
    disk = _CACHE_DIR / (_slug(printer) + ".xml")
    text = None
    if not refresh and disk.exists() and time.time() - disk.stat().st_mtime < _CACHE_MAX_AGE:
        text = disk.read_text(encoding="utf-8", errors="ignore")
    if text is None:
        fd, path = tempfile.mkstemp(prefix="caps_", suffix=".xml")
        os.close(fd)
        try:
            _run_ps("caps", printer, path)
            text = Path(path).read_text(encoding="utf-8", errors="ignore").lstrip("﻿")
            _CACHE_DIR.mkdir(exist_ok=True)
            disk.write_text(text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("could not read the Print Schema of %r", printer, exc_info=True)
            if disk.exists():                                   # stale copy is better than nothing
                text = disk.read_text(encoding="utf-8", errors="ignore")
            else:
                return None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    try:
        caps = Caps(printer, text)
    except ET.ParseError:
        logger.warning("Print Schema of %r is not valid XML", printer)
        return None
    with _lock:
        _cache[printer] = (time.time(), caps)
    return caps


def peek(printer: str) -> Optional[Caps]:
    """Cached capabilities only (memory, then disk) - never starts PowerShell, so it is safe inside a request."""
    with _lock:
        hit = _cache.get(printer)
    if hit:
        return hit[1]
    disk = _CACHE_DIR / (_slug(printer) + ".xml")
    if disk.exists():
        try:
            caps = Caps(printer, disk.read_text(encoding="utf-8", errors="ignore"))
        except ET.ParseError:
            return None
        with _lock:
            _cache[printer] = (time.time() - _CACHE_MAX_AGE + 300, caps)   # re-read in the background soon
        return caps
    return None


def load_in_background(printers: List[str]) -> None:
    """Read (or refresh) the capabilities of every shared printer without delaying start-up."""
    def work() -> None:
        for name in printers:
            try:
                caps = load(name, refresh=True)
                if caps:
                    logger.info("Driver capabilities of %r: %s", name, json.dumps(caps.summary(), default=str)[:600])
                else:
                    logger.warning("No Print Schema for %r - driver features will not be offered", name)
            except Exception:  # noqa: BLE001
                logger.exception("could not read driver capabilities of %r", name)
    threading.Thread(target=work, name="driver-caps", daemon=True).start()
