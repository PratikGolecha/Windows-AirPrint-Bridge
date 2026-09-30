"""
Ink / toner levels for AirPrint Bridge.

Windows does not expose supply levels of a USB printer in a standard way (Canon's status monitor uses a private
channel), so levels come from the printer itself when it has a network side: set ``"supplies_ip": "192.168.0.x"``
(and optionally ``"supplies_path": "/ipp/print"``) in the printer's config and the bridge asks that address with
IPP Get-Printer-Attributes (marker-names / marker-levels / marker-colors / marker-types).  Works for any brand that
implements IPP Everywhere (Brother, HP, Epson, Canon network models).  Without it, levels are reported as unknown.
"""
from __future__ import annotations

import logging
import struct
import threading
import time
import urllib.request
from typing import List, Optional

logger = logging.getLogger("airprint_bridge")
_cache: dict = {}
_lock = threading.Lock()
_WANT = ("marker-names", "marker-levels", "marker-colors", "marker-types", "marker-low-levels",
         "marker-high-levels", "printer-state-reasons")


def _attr(tag: int, name: str, value: bytes) -> bytes:
    n = name.encode()
    return struct.pack("!BH", tag, len(n)) + n + struct.pack("!H", len(value)) + value


def _request(uri: str) -> bytes:
    body = struct.pack("!BBHI", 1, 1, 0x000B, 1) + b"\x01"
    body += _attr(0x47, "attributes-charset", b"utf-8") + _attr(0x48, "attributes-natural-language", b"en")
    body += _attr(0x45, "printer-uri", uri.encode())
    for i, w in enumerate(_WANT):
        body += (_attr(0x44, "requested-attributes", w.encode()) if i == 0
                 else struct.pack("!BH", 0x44, 0) + struct.pack("!H", len(w)) + w.encode())
    return body + b"\x03"


def _parse(data: bytes) -> dict:
    out: dict = {}
    i, last = 8, None
    while i < len(data):
        tag = data[i]
        i += 1
        if tag <= 0x05:
            if tag == 0x03:
                break
            continue
        nl = struct.unpack("!H", data[i:i + 2])[0]
        i += 2
        name = data[i:i + nl].decode("ascii", "replace")
        i += nl
        vl = struct.unpack("!H", data[i:i + 2])[0]
        i += 2
        raw = data[i:i + vl]
        i += vl
        if name:
            last = name
        if tag in (0x21, 0x23) and vl == 4:
            val = struct.unpack("!i", raw)[0]
        elif tag >= 0x40:
            val = raw.decode("utf-8", "replace")
        else:
            continue
        if last:
            out.setdefault(last, []).append(val)
    return out


def _fetch(ip: str, path: str) -> Optional[List[dict]]:
    uri = f"ipp://{ip}:631{path}"
    req = urllib.request.Request(f"http://{ip}:631{path}", data=_request(uri),
                                 headers={"Content-Type": "application/ipp"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        attrs = _parse(resp.read())
    names = attrs.get("marker-names", [])
    if not names:
        return []
    levels, colors, types = attrs.get("marker-levels", []), attrs.get("marker-colors", []), attrs.get("marker-types", [])
    low = attrs.get("marker-low-levels", [])
    return [{"name": n, "level": levels[k] if k < len(levels) else -1, "color": colors[k] if k < len(colors) else "",
             "type": types[k] if k < len(types) else "", "low": low[k] if k < len(low) else -1}
            for k, n in enumerate(names)]


def get(cfg: Optional[dict], max_age: float = 60.0) -> dict:
    """{'available': bool, 'source': str, 'items': [{name, level 0-100 or -1/-2/-3, color, type, low}], 'note': str}"""
    cfg = cfg or {}
    ip = str(cfg.get("supplies_ip") or "")
    if not ip:
        return {"available": False, "items": [], "source": "",
                "note": "Not available: this printer is connected by USB and Windows does not report its ink levels. "
                        "Give the printer a network address and set supplies_ip in the settings to read them over IPP."}
    with _lock:
        hit = _cache.get(ip)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
    try:
        items = _fetch(ip, str(cfg.get("supplies_path") or "/ipp/print"))
        res = {"available": bool(items), "items": items or [], "source": f"ipp://{ip}",
               "note": "" if items else "The printer answered but reports no supply levels."}
    except Exception as exc:  # noqa: BLE001
        logger.debug("supplies query to %s failed: %s", ip, exc)
        res = {"available": False, "items": [], "source": f"ipp://{ip}", "note": f"Could not reach {ip}: {exc}"}
    with _lock:
        _cache[ip] = (time.time(), res)
    return res
