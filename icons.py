"""
Printer pictures for AirPrint Bridge.

Phones (Android print service / Mopria, iOS) show a picture of the printer when the printer lists PNG addresses in its IPP
``printer-icons`` attribute (48, 128 and 512 px).  A Brother shows its own product photo; a printer behind the bridge has none, so the
bridge draws a simple flat picture that matches the kind of printer, or serves your own PNG (``"icon_file": "C:\\path\\photo.png"`` in that
printer's config).  Kinds: ``inkjet`` (ink-tank printer, optional scanner lid), ``laser`` (mono/colour laser with a copier lid),
``label`` (thermal label printer).  ``"icon": "<kind>"`` in the config overrides the automatic choice.
"""
from __future__ import annotations

import io
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw

_cache: Dict[Tuple[str, int], bytes] = {}

BODY, BODY_D, DARK, PANEL, PAPER, EDGE = "#eceef2", "#c9cdd6", "#4a4f5a", "#2b2f38", "#ffffff", "#8a909c"


def _rr(d: ImageDraw.ImageDraw, box, r, fill, outline=EDGE, w=3):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=w)


def _draw(kind: str, scanner: bool, color: bool) -> Image.Image:
    S = 1024                                                    # drawn large, then shrunk (smooth edges)
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((120, 800, 904, 900), fill=(0, 0, 0, 40))          # soft floor shadow

    if kind == "label":
        _rr(d, (250, 470, 774, 800), 44, BODY)                    # body
        _rr(d, (250, 470, 774, 560), 44, BODY_D)                  # lid
        _rr(d, (300, 610, 470, 650), 12, PANEL, PANEL)            # slot / display
        d.ellipse((660, 610, 700, 650), fill="#3aa757", outline="#2d8a46", width=2)     # power led
        _rr(d, (330, 300, 694, 470), 12, PAPER, EDGE, 4)          # label coming out (top)
        x = 356
        for w in (10, 4, 14, 6, 6, 12, 4, 10, 14, 5, 8, 12, 4, 9):                       # barcode
            d.rectangle((x, 340, x + w, 430), fill=DARK); x += w + 6
        d.rectangle((356, 442, 660, 452), fill=EDGE)
        d.rectangle((250, 780, 774, 800), fill=BODY_D)
        return im

    # body common to inkjet / laser
    top = 300 if not scanner else 250
    if scanner:                                                   # scanner / copier lid
        _rr(d, (200, 250, 824, 350), 22, BODY_D)
        _rr(d, (230, 268, 794, 332), 12, "#dfe6ee" if kind == "inkjet" else "#e6ebf0", EDGE, 2)
        d.polygon([(250, 320), (330, 272), (470, 272), (390, 320)], fill=(255, 255, 255, 110))
    _rr(d, (170, 350, 854, 780), 52, BODY)                        # main body
    _rr(d, (170, 350, 854, 420), 40, BODY_D)                      # upper band
    _rr(d, (640, 372, 810, 402), 10, PANEL, PANEL)                # small display
    d.ellipse((606, 376, 632, 402), fill="#3aa757", outline="#2d8a46", width=2)
    # paper output tray (front) with a sheet
    _rr(d, (270, 700, 754, 800), 20, BODY_D)
    _rr(d, (300, 620, 724, 730), 10, PAPER, EDGE, 3)
    for y in (650, 680, 710):
        d.rectangle((330, y, 694 - (30 if y == 710 else 0), y + 8), fill="#cfd3da")
    if kind == "inkjet":                                          # front ink-tank window with four tanks
        _rr(d, (250, 450, 774, 590), 20, "#f7f9fc", EDGE, 3)
        cols = ["#111111", "#00b7eb", "#ff2d95", "#ffd400"] if color else ["#111111"]
        n = len(cols); span = 470 // max(n, 1)
        for i, c in enumerate(cols):
            cx = 280 + i * span + (span - 70) // 2 if n > 1 else 470
            _rr(d, (cx, 470, cx + 70, 570), 10, "#ffffff", EDGE, 2)
            d.rectangle((cx + 6, 505, cx + 64, 564), fill=c)
    else:                                                         # laser: front cassette line + button panel
        _rr(d, (250, 450, 774, 600), 16, BODY_D)
        d.rectangle((250, 520, 774, 526), fill=EDGE)
    return im


def make_icon(kind: str, size: int, scanner: bool = False, color: bool = True) -> bytes:
    kind = kind if kind in ("inkjet", "laser", "label") else "laser"
    key = (f"{kind}:{int(scanner)}:{int(color)}", size)
    if key not in _cache:
        img = _draw(kind, scanner, color).resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        _cache[key] = buf.getvalue()
    return _cache[key]


def file_icon(path: str, size: int) -> Optional[bytes]:
    """Your own picture, resized to a square PNG of *size* px (kept in proportion, transparent padding)."""
    try:
        im = Image.open(path).convert("RGBA")
        im.thumbnail((size, size), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
        buf = io.BytesIO()
        canvas.save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def kind_for(cfg: Optional[dict], has_scanner: bool, is_color: bool, is_label: bool) -> str:
    k = str((cfg or {}).get("icon") or "").lower()
    if k in ("inkjet", "laser", "label"):
        return k
    if is_label:
        return "label"
    return "inkjet" if is_color else "laser"
