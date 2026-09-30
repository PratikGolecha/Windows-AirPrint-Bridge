#!/usr/bin/env python3
"""
AirPrint / IPP Bridge Server for Windows
=========================================

Advertises a PC-connected USB printer on the local network via mDNS (DNS-SD)
so that iOS (AirPrint) and Android (IPP Everywhere) devices can discover and
print to it natively — no mobile apps required.

Architecture
------------
1. Zeroconf broadcasts ``_ipp._tcp.local.`` on the LAN.
2. A lightweight HTTP server on port 631 accepts IPP ``Print-Job`` requests.
3. The document payload (PDF / JPEG / PNG) is extracted from the binary IPP
   envelope and spooled to the default Windows printer via the Win32 API.

Author : Salman Asmat
Created: 2026-08-10
License: GPL-3.0
"""

from __future__ import annotations

import atexit
import hashlib
import html
import logging
import os
import re
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Tuple

try:                                  # optional scanner (eSCL/AirScan) support
    import escl_scanner
except ImportError:                   # pragma: no cover
    escl_scanner = None
# Load the printing libraries ONCE, here in the main thread.  Importing them for the first time from a background
# print thread stalled forever (win32ui's DLL load), which left jobs stuck in "processing".
try:
    import pythoncom
    import win32con
    import win32ui
    import fitz
    from PIL import Image, ImageWin
except ImportError:                    # spool_to_printer reports a clear error if one is really missing
    pass

import job_tracking
import webui                       # web admin page + JSON API (webui.py)
import icons                       # drawn printer pictures for phones (icons.py)
import maintenance                 # nozzle check / cleaning ... as RAW jobs (maintenance.py)
import supplies                    # ink/toner levels from the printer's network side (supplies.py)
import driver_caps                 # what the printer's own driver can do (media types, borderless, quality ...)

JOBS = job_tracking.JobTracker(path=str(Path(sys.executable).resolve().parent / 'jobs.json') if getattr(sys, 'frozen', False) else str(Path(__file__).resolve().parent / 'jobs.json'))       # IPP job ids/states (see job_tracking.py)

try:
    import wsd_device
except ImportError:      # optional
    wsd_device = None

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
try:
    from zeroconf import ServiceInfo, Zeroconf
except ImportError:
    raise SystemExit(
        "Missing dependency: zeroconf\n"
        "Install with:  pip install zeroconf"
    )

try:
    import win32api
    import win32gui
    import win32print
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
except ImportError:
    raise SystemExit(
        "Missing dependency: pywin32\n"
        "Install with:  pip install pywin32"
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION: str = "1.3.1"
IPP_PORT: int = 631
IPP_SERVICE_TYPE: str = "_ipp._tcp.local."

# IPP binary protocol constants
IPP_VERSION_MAJOR: int = 1
IPP_VERSION_MINOR: int = 1

# IPP operation IDs we handle
IPP_OP_PRINT_JOB: int = 0x0002
IPP_OP_VALIDATE_JOB: int = 0x0004
IPP_OP_CANCEL_JOB: int = 0x0008
IPP_OP_GET_JOB_ATTRIBUTES: int = 0x0009
IPP_OP_GET_JOBS: int = 0x000A
IPP_OP_GET_PRINTER_ATTRIBUTES: int = 0x000B

# IPP status codes
IPP_STATUS_OK: int = 0x0000
IPP_STATUS_OK_IGNORED: int = 0x0001
IPP_STATUS_BAD_REQUEST: int = 0x0400
IPP_STATUS_NOT_FOUND: int = 0x0406
IPP_STATUS_INTERNAL_ERROR: int = 0x0500

# IPP attribute tags
IPP_TAG_OPERATION: int = 0x01
IPP_TAG_JOB: int = 0x02
IPP_TAG_END: int = 0x03
IPP_TAG_PRINTER: int = 0x04
IPP_TAG_UNSUPPORTED: int = 0x05

# IPP value tags
IPP_TAG_INTEGER: int = 0x21
IPP_TAG_BOOLEAN: int = 0x22
IPP_TAG_ENUM: int = 0x23
IPP_TAG_RANGE: int = 0x33
IPP_TAG_BEG_COLLECTION: int = 0x34
IPP_TAG_END_COLLECTION: int = 0x37
IPP_TAG_TEXT: int = 0x41
IPP_TAG_NAME: int = 0x42
IPP_TAG_KEYWORD: int = 0x44
IPP_TAG_URI: int = 0x45
IPP_TAG_URISCHEME: int = 0x46
IPP_TAG_CHARSET: int = 0x47
IPP_TAG_LANGUAGE: int = 0x48
IPP_TAG_MIMETYPE: int = 0x49
IPP_TAG_MEMBER_ATTR_NAME: int = 0x4A

# File‑type magic bytes
MAGIC_PDF: bytes = b"%PDF"
MAGIC_JPEG: bytes = b"\xFF\xD8\xFF"
MAGIC_PNG: bytes = b"\x89PNG"
MAGIC_URF: bytes = b"UNIRAST"   # Apple Raster (URF) format

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
# When frozen by PyInstaller, __file__ resolves to a temp _MEIXXXXXX dir.
# Use the executable's directory instead so logs persist across restarts.
if getattr(sys, "frozen", False):
    _app_dir = Path(sys.executable).resolve().parent
else:
    _app_dir = Path(__file__).resolve().parent

LOG_FILE: str = str(_app_dir / "airprint_bridge.log")

logger = logging.getLogger("airprint_bridge")
logger.setLevel(logging.DEBUG)

import logging.handlers
_file_handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=4, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(
    logging.Formatter(
        fmt="%(asctime)s  [%(levelname)-8s]  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logger.addHandler(_file_handler)

# Prevent any output to stdout/stderr (headless-safe for pythonw.exe)
logging.getLogger().handlers = []


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def get_local_ip() -> str:
    """Return the primary LAN IPv4 address of this machine."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Does not actually send data; used to determine the outbound iface.
        sock.connect(("8.8.8.8", 80))
        ip: str = sock.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        sock.close()
    logger.info("Detected local IP address: %s", ip)
    return ip


def detect_file_type(data: bytes) -> Tuple[str, str]:
    """
    Sniff the first few bytes of *data* and return ``(extension, mime_type)``.

    Falls back to ``('.bin', 'application/octet-stream')`` for unknown types.
    """
    if data[:4] == MAGIC_PDF:
        return ".pdf", "application/pdf"
    if data[:4] == b"RaS2":
        return ".pwg", "image/pwg-raster"
    if data[:7] == MAGIC_URF:
        return ".urf", "image/urf"
    if data[:3] == MAGIC_JPEG:
        return ".jpg", "image/jpeg"
    if data[:4] == MAGIC_PNG:
        return ".png", "image/png"
    return ".bin", "application/octet-stream"


def convert_urf_to_pdf(urf_path: str) -> str:
    """
    Convert an Apple Raster (URF) file to PDF so Windows can print it.

    URF format: 'UNIRAST\x00' (8-byte header) followed by one or more
    page records.  Each page record has a 4-byte big-endian page header
    length, then pixel data.  We extract page images and compose them
    into a simple PDF.

    If the ``Pillow`` library is available, we decode the raster pages
    into images and wrap them in a PDF.  Otherwise, we fall back to
    writing raw bytes as a single-page PDF image.
    """
    pdf_path = urf_path.rsplit(".", 1)[0] + ".pdf"
    try:
        # Attempt conversion with Pillow (best quality)
        from PIL import Image
        import io

        with open(urf_path, "rb") as f:
            data = f.read()

        # URF header: 'UNIRAST\x00' (8 bytes) then page count (4 bytes BE)
        if len(data) < 12 or data[:8] != b"UNIRAST\x00":
            logger.warning("URF file has invalid header, treating as raw")
            return urf_path

        # Skip the 8-byte magic + 4-byte page count to the page data.
        # Each page has a 32-byte page header, then raw pixel data.
        # For a simpler approach, we try to find embedded JPEG or image data.
        offset = 12  # past magic + page count

        images = []
        while offset < len(data):
            # Page header is typically 32 bytes
            if offset + 32 > len(data):
                break
            # Bytes 16-19: width (BE), bytes 20-23: height (BE)
            # Byte 0: bits per pixel / color space info
            page_header = data[offset:offset + 32]
            bpp = page_header[0]  # bits per component
            color_space = page_header[1]  # 1=sGray, 3=sRGB
            # Duplex/quality fields at bytes 2,3
            width = struct.unpack("!I", page_header[16:20])[0]
            height = struct.unpack("!I", page_header[20:24])[0]
            # Resolution at bytes 8-11 (dpi)
            dpi = struct.unpack("!I", page_header[8:12])[0]
            if dpi == 0:
                dpi = 300

            offset += 32  # advance past page header

            # Determine channels and bytes per pixel
            if color_space == 1:
                channels = 1
                mode = "L"
            else:
                channels = 3
                mode = "RGB"

            row_bytes = width * channels
            page_data = bytearray()

            # URF uses a simple run-length encoding per row
            for _row in range(height):
                if offset >= len(data):
                    break
                row = bytearray()
                while len(row) < row_bytes:
                    if offset >= len(data):
                        break
                    count_byte = data[offset]
                    offset += 1
                    if count_byte == 0:
                        # Repeat the next pixel (count+1) times
                        # count_byte 0 = 1 repeat of next pixel
                        if offset + channels > len(data):
                            break
                        pixel = data[offset:offset + channels]
                        offset += channels
                        row.extend(pixel)
                    elif count_byte <= 127:
                        # Repeat next pixel (count_byte + 1) times
                        if offset + channels > len(data):
                            break
                        pixel = data[offset:offset + channels]
                        offset += channels
                        row.extend(pixel * (count_byte + 1))
                    else:
                        # (257 - count_byte) literal pixels follow
                        literal_count = 257 - count_byte
                        literal_bytes = literal_count * channels
                        if offset + literal_bytes > len(data):
                            break
                        row.extend(data[offset:offset + literal_bytes])
                        offset += literal_bytes
                # Pad or truncate to exact row width
                page_data.extend(row[:row_bytes])

            if width > 0 and height > 0 and len(page_data) >= row_bytes:
                actual_height = min(height, len(page_data) // row_bytes)
                img = Image.frombytes(
                    mode, (width, actual_height),
                    bytes(page_data[:actual_height * row_bytes]),
                )
                images.append(img)
                logger.info(
                    "URF page decoded: %dx%d %s @ %d dpi",
                    width, actual_height, mode, dpi,
                )

        if images:
            # Save all pages as a multi-page PDF
            first = images[0]
            if len(images) > 1:
                first.save(
                    pdf_path, "PDF", save_all=True,
                    append_images=images[1:], resolution=dpi,
                )
            else:
                first.save(pdf_path, "PDF", resolution=dpi)
            logger.info("URF converted to PDF: %s (%d pages)", pdf_path, len(images))
            return pdf_path

    except ImportError:
        logger.warning(
            "Pillow not installed — cannot convert URF to PDF. "
            "Install with: pip install Pillow"
        )
    except Exception:
        logger.exception("URF→PDF conversion failed")

    # Fallback: return original URF path (ShellExecute may still work
    # if the user has a URF-capable viewer installed)
    return urf_path


def get_default_printer() -> str:
    """Return the name of the Windows default printer."""
    name: str = win32print.GetDefaultPrinter()
    logger.info("Default Windows printer: %s", name)
    return name


def _load_config() -> dict:
    """Return the parsed ``config.json`` next to the script/exe (or ``{}``)."""
    import json

    config_path = _app_dir / "config.json"
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.critical("Cannot read %s: %s", config_path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def get_display_name(printer_name: str, cfg: Optional[dict] = None) -> str:
    """
    Name shown to phones and tablets.

    Defaults to the plain Windows printer name (no PC-name suffix).  Set
    ``"display_name"`` in config.json to show something different.
    """
    return str((cfg if cfg is not None else _load_config()).get("display_name") or printer_name)


_MEDIA_ALIASES = {
    "a4": "iso_a4_210x297mm", "a5": "iso_a5_148x210mm", "a6": "iso_a6_105x148mm",
    "letter": "na_letter_8.5x11in", "legal": "na_legal_8.5x14in",
}


def get_default_paper(cfg: Optional[dict] = None) -> Optional[Tuple[str, Tuple[float, float]]]:
    """
    ``"default_paper": "A4"`` in config.json = the paper to use when the SENDER DOES NOT CHOOSE a size.
    A job that names a size (Letter, 4x6, A5 ...) is always respected.  Without this setting a job with no
    size copies the page size of the document itself - so a Letter file makes the printer stop and wait for
    Letter paper.  Leave it unset on label printers (where copying the document size is what you want).
    Accepts A4/A5/A6/Letter/Legal or a full IPP media keyword.  Returns (ipp_keyword, (w_mm, h_mm)) or None.
    """
    value = str((cfg if cfg is not None else _load_config()).get("default_paper") or "").strip()
    if not value:
        return None
    key = _MEDIA_ALIASES.get(value.lower(), value)
    size = IPP_MEDIA_SIZES.get(key) or size_from_keyword(key)
    if not size:
        logger.warning("config.json default_paper=%r is not a known size - ignoring it", value)
        return None
    return key, size


def choose_media_size(
    requested_mm: Optional[Tuple[float, float]], sender_chose: bool, cfg: Optional[dict] = None,
) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
    """Return (size_to_use, note).  The configured default applies ONLY when the sender named no size."""
    if requested_mm is not None or sender_chose:
        return requested_mm, None
    default = get_default_paper(cfg)
    if default:
        return default[1], default[0]
    return None, None


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "printer"


def get_printer_configs() -> list:
    """
    The printers to share.  ``config.json`` either has the usual single-printer keys at the top level
    (one printer - the original behaviour) or a ``"printers": [ {...}, {...} ]`` list, each entry with its own
    ``printer``, ``display_name``, ``scanner``, ``default_paper``, ``wsd_model``, ``wsd_maker``, ``wsd_uuid``.
    Top-level ``wsd`` / ``wsd_port`` apply to all.  The FIRST printer also answers the old un-prefixed URLs
    (/ipp/print, /eSCL, /WebServices) so existing queues keep working; every printer answers /<id>/...
    """
    base = _load_config()
    entries = base.get("printers")
    multi = isinstance(entries, list) and bool(entries)
    if not multi:
        entries = [{}]
    out, seen = [], set()
    for e in entries:
        if multi:      # a printers[] list: only the shared keys carry over (never one printer's scanner/uuid to another)
            cfg = {k: base[k] for k in ("wsd", "wsd_port") if k in base}
        else:
            cfg = {k: v for k, v in base.items() if k != "printers"}
        cfg.update(e if isinstance(e, dict) else {})
        name = str(cfg.get("printer") or "")
        if not name:
            name = get_default_printer() if not out else ""
        if not name:
            logger.critical("A printers[] entry has no 'printer' name - skipped")
            continue
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        if name not in [p[2] for p in win32print.EnumPrinters(flags)]:
            logger.critical("Configured printer %r not found - skipped", name)
            continue
        pid = _slug(str(cfg.get("id") or cfg.get("display_name") or name))
        while pid in seen or pid in ("ipp", "escl", "webservices"):
            pid += "-2"
        seen.add(pid)
        cfg["printer"], cfg["id"] = name, pid
        out.append(cfg)
    return out


def printer_is_color(printer_name: str, cfg: Optional[dict] = None) -> bool:
    """Is this a colour printer?  config ``"color": true|false`` wins; otherwise ask the Windows driver."""
    if cfg is not None and cfg.get("color") is not None:
        return bool(cfg.get("color"))
    try:
        return win32print.DeviceCapabilities(printer_name, "", 32) == 1          # 32 = DC_COLORDEVICE
    except Exception:  # noqa: BLE001
        return True


def printer_traits(printer_name: str, cfg: Optional[dict] = None) -> dict:
    """What this printer really is, for every place the bridge announces it (IPP, mDNS, Windows WSD):
    {'color': bool, 'duplex': bool, 'urf': 'AirPrint raster string'}.  Colour comes from the driver (or config "color"),
    two-sided from the driver's automatic-duplex option (or config "duplex"), resolutions from the driver."""
    color = printer_is_color(printer_name, cfg)
    caps = driver_caps.peek(printer_name)
    if cfg is not None and cfg.get("duplex") is not None:
        duplex = bool(cfg.get("duplex"))
    else:
        duplex = bool(caps and caps.duplex_options())
    res = sorted({max(r) for r in caps.resolutions()}) if caps else []
    res = [r for r in res if r <= 1200] or [300, 600]
    rs = f"RS{res[0]}" if len(res) == 1 else f"RS{res[0]}-{res[-1]}"
    parts = ["V1.4", "W8"] + (["SRGB24"] if color else []) + [rs] + (["DM1"] if duplex else [])
    return {"color": color, "duplex": duplex, "urf": ",".join(parts)}


def get_target_printer() -> str:
    """
    Return the printer to share.

    If a ``config.json`` next to the script/exe contains ``{"printer": "<name>"}``
    that exact Windows printer name is used (and must exist).  Otherwise the
    Windows default printer is used, as before.
    """
    if (_app_dir / "config.json").is_file():
        configured = _load_config().get("printer", "")
        if configured:
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            installed = [p[2] for p in win32print.EnumPrinters(flags)]
            if configured not in installed:
                logger.critical(
                    "Configured printer %r not found. Installed printers: %s",
                    configured, installed,
                )
                return ""
            logger.info("Using printer from config.json: %s", configured)
            return configured
    return get_default_printer()


def _select_pages(total: int, ranges: str) -> list:
    """0-based page indexes for an IPP page-ranges string like '1-3,5-5' (empty = all pages)."""
    if not ranges:
        return list(range(total))
    wanted = set()
    for part in str(ranges).split(","):
        try:
            lo, hi = (int(x) for x in part.split("-"))
        except ValueError:
            continue
        wanted.update(range(max(1, lo), min(hi, total) + 1))
    return [i for i in range(total) if (i + 1) in wanted] or list(range(total))


def _best_grid(n: int, sheet_w: float, sheet_h: float, page_w: float, page_h: float) -> Tuple[int, int]:
    """(columns, rows) that make n pages as large as possible on the sheet."""
    best, best_key = (1, n), (-1.0, 0)
    for cols in range(1, n + 1):
        rows = -(-n // cols)
        scale = min(sheet_w / cols / page_w, sheet_h / rows / page_h)
        key = (round(scale, 4), -(cols * rows))
        if key > best_key:
            best, best_key = (cols, rows), key
    return best


def job_options(attrs: dict) -> dict:
    """The per-job choices the bridge applies itself or hands to the driver, from the parsed IPP attributes."""
    margins = [attrs.get(k) for k in ("media-top-margin", "media-bottom-margin", "media-left-margin", "media-right-margin")]
    named = "borderless" in str(attrs.get("media", "")).lower()          # the sender picked a paper size called "... borderless": always honoured
    borderless = named or all(m == "0" for m in margins)
    if borderless and not named and attrs.get("x-client-mobile") == "1":
        # a phone that merely sent zero margins: only photo-size paper (phones have no borderless switch; they'd get it by accident)
        dims = None
        if attrs.get("x-dimension") and attrs.get("y-dimension"):
            try:
                dims = (int(attrs["x-dimension"]) / 100.0, int(attrs["y-dimension"]) / 100.0)
            except ValueError:
                dims = None
        if dims is None:
            kw = re.sub(r"[._-]borderless$", "", str(attrs.get("media", "")), flags=re.I)
            dims = IPP_MEDIA_SIZES.get(kw) or size_from_keyword(kw)
        if dims and not is_photo_size(*dims):
            logger.info("Zero margins requested for a %.0f x %.0f mm sheet - printing normally (borderless is only for photo sizes)", *dims)
            borderless = False
    try:
        number_up = int(attrs.get("number-up", "1") or 1)
    except ValueError:
        number_up = 1
    scaling = attrs.get("print-scaling", "auto")
    return {"media_type": attrs.get("media-type", ""), "borderless": borderless,
            "sides": attrs.get("sides", "one-sided"), "media_source": attrs.get("media-source", ""),
            "reverse": attrs.get("page-delivery", "") == "reverse-order",
            "uncollated": attrs.get("sheet-collate", "") == "uncollated",
            "page_ranges": attrs.get("page-ranges", ""), "number_up": number_up,
            "scaling": scaling if scaling in ("auto", "fit", "fill", "none") else "auto"}


def _dc_from_driver_features(printer_name: str, media_size_mm, color_mode: str, quality: str, opts: dict):
    """Printer DC built from the driver's own settings (media type, borderless, quality ...) via a PrintTicket.
    None when nothing special was asked for, the driver has no Print Schema, or Windows refused the combination -
    the caller then uses its plain DEVMODE path."""
    two_sided = opts.get("sides", "one-sided") not in ("", "one-sided")
    tray = opts.get("media_source") not in ("", "auto", None)
    wants = opts.get("media_type") or opts.get("borderless") or str(quality) in ("3", "5") or two_sided or tray
    if not wants:
        return None
    try:
        caps = driver_caps.load(printer_name)
        if caps is None:
            return None
        choices = {}
        mt = caps.media_type_option(opts.get("media_type", "")) if opts.get("media_type") else None
        if mt:
            choices["psk:PageMediaType"] = mt
        if opts.get("borderless") and caps.borderless_supported():
            choices["psk:PageBorderless"] = next(o["name"] for o in caps.options("psk:PageBorderless")
                                                  if o["name"].lower().endswith(":borderless"))
        q = caps.quality_options().get(int(quality)) if str(quality).isdigit() else None
        if q:
            choices["psk:PageOutputQuality"] = q
        if two_sided:
            d = caps.duplex_options().get(opts["sides"])
            if d:
                choices["psk:JobDuplexAllDocumentsContiguously"] = d
        if tray:
            b = caps.input_bin_option(opts["media_source"])
            if b:
                choices["psk:JobInputBin"] = b
        if color_mode in ("color", "monochrome"):
            c = caps.color_option(color_mode == "monochrome")
            if c:
                choices["psk:PageOutputColor"] = c
        if media_size_mm:
            w, h = media_size_mm
            so = caps.size_option(min(w, h), max(w, h))
            if so:
                choices["psk:PageMediaSize"] = so
            if "psk:PageOrientation" in caps.features:
                choices["psk:PageOrientation"] = "psk:Landscape" if w > h else "psk:Portrait"
        if not choices:
            return None
        dm = caps.devmode(choices)
        if not dm:
            return None
        import ctypes
        from ctypes import wintypes
        gdi = ctypes.windll.gdi32
        gdi.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
        gdi.CreateDCW.restype = wintypes.HDC
        buf = ctypes.create_string_buffer(dm, len(dm))
        handle = gdi.CreateDCW("WINSPOOL", printer_name, None, ctypes.cast(buf, ctypes.c_void_p))
        if not handle:
            logger.warning("CreateDC with the driver settings failed - using plain settings")
            return None
        logger.info("Printer DC built from driver settings %s", choices)
        return win32ui.CreateDCFromHandle(handle)
    except Exception:  # noqa: BLE001
        logger.exception("could not apply the driver's own settings - using plain settings")
        return None


def spool_to_printer(
    file_path: str,
    printer_name: str,
    media_size_mm: Optional[Tuple[float, float]] = None,
    color_mode: str = "",
    copies: int = 1,
    quality: str = "",
    media_type: str = "",
    doc_name: str = "",
    opts: Optional[dict] = None,
) -> None:
    """
    Send *file_path* to the Windows print queue of *printer_name*.

    Uses PyMuPDF to rasterize the PDF and win32ui / win32gui to send it
    directly to the printer's Device Context (DC) configured for the
    correct physical paper size.
    """
    logger.info("Spooling '%s' to printer '%s'", file_path, printer_name)

    try:
        import win32print
        import win32gui
        import win32ui
        import win32con
        import pythoncom
        import fitz
        from PIL import Image, ImageWin
    except ImportError as e:
        logger.error("Missing dependency for printing: %s", e)
        raise

    pythoncom.CoInitialize()
    try:
        # Auto-detect media size from PDF document if not explicitly supplied
        if media_size_mm is None and file_path.lower().endswith(".pdf"):
            try:
                _inspect_doc = fitz.open(file_path)
                if len(_inspect_doc) > 0:
                    _p0 = _inspect_doc.load_page(0)
                    _pdf_w_mm = _p0.rect.width * 25.4 / 72.0
                    _pdf_h_mm = _p0.rect.height * 25.4 / 72.0
                    media_size_mm = (_pdf_w_mm, _pdf_h_mm)
                    logger.info(
                        "No IPP media specified — auto-detected media size from PDF: %.1f × %.1f mm",
                        _pdf_w_mm, _pdf_h_mm,
                    )
                _inspect_doc.close()
            except Exception:
                logger.exception("Failed to auto-detect media size from PDF")

        if media_size_mm:
            logger.info(
                "Target media size: %.1f × %.1f mm",
                media_size_mm[0], media_size_mm[1],
            )

        hprinter = win32print.OpenPrinter(printer_name)
        try:
            # ----------------------------------------------------------
            # Configure DEVMODE to match the target paper size
            # ----------------------------------------------------------
            devmode = None
            opts = opts or {}
            hdc = _dc_from_driver_features(printer_name, media_size_mm, color_mode, quality, opts)
            if media_size_mm and hdc is None:
                try:
                    width_mm, height_mm = media_size_mm
                    props = win32print.GetPrinter(hprinter, 2)
                    devmode = props["pDevMode"]

                    # Set orientation: Landscape if wider than tall, else Portrait
                    if width_mm > height_mm:
                        devmode.Orientation = win32con.DMORIENT_LANDSCAPE
                    else:
                        devmode.Orientation = win32con.DMORIENT_PORTRAIT
                    devmode.Fields |= win32con.DM_ORIENTATION

                    # Search printer's supported paper forms for a size match
                    matched_id = None
                    try:
                        papers = win32print.DeviceCapabilities(printer_name, "", win32con.DC_PAPERS)
                        sizes = win32print.DeviceCapabilities(printer_name, "", win32con.DC_PAPERSIZE)
                        names = win32print.DeviceCapabilities(printer_name, "", win32con.DC_PAPERNAMES)
                        for pid, s, name in zip(papers, sizes, names):
                            pw = s["x"] / 10.0
                            ph = s["y"] / 10.0
                            if (abs(pw - width_mm) < 3.0 and abs(ph - height_mm) < 3.0) or \
                               (abs(pw - height_mm) < 3.0 and abs(ph - width_mm) < 3.0):
                                matched_id = pid
                                logger.info(
                                    "Matched printer form ID=%d (%s) for %.1f × %.1f mm",
                                    pid, name.strip(), width_mm, height_mm,
                                )
                                break
                    except Exception:
                        logger.debug("DeviceCapabilities lookup failed, trying standard forms")

                    # Fall back to standard Windows DMPAPER IDs
                    if matched_id is None:
                        standard_paper_map = {
                            (210.0, 297.0): win32con.DMPAPER_A4,
                            (148.0, 210.0): win32con.DMPAPER_A5,
                            (105.0, 148.0): win32con.DMPAPER_A6,
                            (105.0, 148.5): win32con.DMPAPER_A6,
                            (297.0, 420.0): win32con.DMPAPER_A3,
                            (215.9, 279.4): win32con.DMPAPER_LETTER,
                            (215.9, 355.6): win32con.DMPAPER_LEGAL,
                        }
                        for (sw, sh), sid in standard_paper_map.items():
                            if (abs(sw - width_mm) < 3.0 and abs(sh - height_mm) < 3.0) or \
                               (abs(sw - height_mm) < 3.0 and abs(sh - width_mm) < 3.0):
                                matched_id = sid
                                logger.info(
                                    "Matched standard Windows form ID=%d for %.1f × %.1f mm",
                                    sid, width_mm, height_mm,
                                )
                                break

                    if matched_id is not None:
                        devmode.PaperSize = matched_id
                        devmode.Fields |= win32con.DM_PAPERSIZE
                    else:
                        devmode.PaperSize = 256  # DMPAPER_USER (custom)
                        devmode.PaperWidth = int(round(width_mm * 10))
                        devmode.PaperLength = int(round(height_mm * 10))
                        devmode.Fields |= (
                            win32con.DM_PAPERSIZE
                            | win32con.DM_PAPERWIDTH
                            | win32con.DM_PAPERLENGTH
                        )

                    # Validate and merge DEVMODE with printer driver
                    try:
                        win32print.DocumentProperties(
                            0, hprinter, printer_name, devmode, devmode,
                            win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
                        )
                    except Exception:
                        logger.warning("DocumentProperties merge failed, using direct DEVMODE")

                    logger.info(
                        "DEVMODE configured: PaperSize=%d, Orientation=%d",
                        devmode.PaperSize, devmode.Orientation,
                    )
                except Exception:
                    logger.exception(
                        "Failed to configure DEVMODE for media size — falling back to driver defaults"
                    )
                    devmode = None

            # Colour / monochrome as chosen by the sender (print-color-mode); "auto" or nothing = the driver default
            quality_dm = {"3": -1, "5": -4}.get(str(quality))            # IPP draft -> DMRES_DRAFT, high -> DMRES_HIGH
            media_dm = 3 if "photo" in media_type or "glossy" in media_type else (1 if media_type in ("stationery", "plain") else 0)
            if hdc is None and (color_mode in ("color", "monochrome") or quality_dm or media_dm):
                try:
                    if devmode is None:
                        devmode = win32print.GetPrinter(hprinter, 2)["pDevMode"]
                    if color_mode in ("color", "monochrome"):
                        devmode.Color = win32con.DMCOLOR_MONOCHROME if color_mode == "monochrome" else win32con.DMCOLOR_COLOR
                        devmode.Fields |= win32con.DM_COLOR
                        logger.info("Print colour mode set to %s", color_mode)
                    if quality_dm:
                        devmode.PrintQuality = quality_dm
                        devmode.Fields |= 0x400                       # DM_PRINTQUALITY
                        logger.info("Print quality set to %s", "draft" if quality_dm == -1 else "high")
                    if media_dm:
                        devmode.MediaType = media_dm
                        devmode.Fields |= 0x2000000                   # DM_MEDIATYPE
                        logger.info("Media type set to %s", media_type)
                except Exception:  # noqa: BLE001
                    logger.exception("could not set colour/quality/media-type - using the driver defaults")

            # ----------------------------------------------------------
            # Create the printer DC (using win32gui.CreateDC with devmode)
            # ----------------------------------------------------------
            if hdc is None and devmode is not None:
                try:
                    hdc_handle = win32gui.CreateDC("WINSPOOL", printer_name, devmode)
                    if hdc_handle:
                        hdc = win32ui.CreateDCFromHandle(hdc_handle)
                        logger.info("Printer DC successfully created with custom DEVMODE")
                except Exception:
                    logger.exception("Failed to create DC with custom DEVMODE, trying default DC")
                    hdc = None

            if hdc is None:
                hdc = win32ui.CreateDC()
                hdc.CreatePrinterDC(printer_name)
                logger.info("Printer DC created with default settings")

            printer_dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX)
            printer_dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
            logger.info("Printer DPI: %d × %d", printer_dpi_x, printer_dpi_y)

            hdc.StartDoc(doc_name or file_path)

            pdf_doc = fitz.open(file_path)
            n_copies = max(1, min(int(copies or 1), 99))
            if n_copies > 1:
                logger.info("Printing %d copies", n_copies)
            pages = _select_pages(len(pdf_doc), opts.get("page_ranges", ""))
            n_up = int(opts.get("number_up") or 1)
            if n_up not in (1, 2, 4, 6, 9, 16):
                n_up = 1
            if opts.get("reverse"):
                pages = pages[::-1]
            sheets = [pages[i:i + n_up] for i in range(0, len(pages), n_up)]
            if opts.get("uncollated") and n_copies > 1:            # 1,1,1,2,2,2 instead of 1,2,1,2
                sheets = [sh for sh in sheets for _ in range(n_copies)]
                n_copies = 1
            scaling = opts.get("scaling") or "auto"
            if opts.get("borderless") and scaling == "auto":
                scaling = "fill"                                   # borderless: cover the whole sheet
            if len(pages) != len(pdf_doc) or n_up > 1 or scaling != "auto":
                logger.info("Pages %s, %d per sheet, scaling=%s", "all" if len(pages) == len(pdf_doc) else
                            f"{len(pages)} of {len(pdf_doc)}", n_up, scaling)
            for _copy in range(n_copies):
                for sheet_no, sheet in enumerate(sheets):
                    logger.info("Rendering sheet %d/%d...", sheet_no + 1, len(sheets))
                    hdc.StartPage()

                    printable_width = hdc.GetDeviceCaps(win32con.HORZRES)
                    printable_height = hdc.GetDeviceCaps(win32con.VERTRES)
                    dpi_scale_x = printer_dpi_x / 72.0
                    dpi_scale_y = printer_dpi_y / 72.0

                    if len(sheet) > 1:
                        # several pages on one sheet: render each into its cell of a grid
                        first_rect = pdf_doc.load_page(sheet[0]).rect
                        cols, rows = _best_grid(len(sheet), printable_width, printable_height,
                                                first_rect.width * dpi_scale_x, first_rect.height * dpi_scale_y)
                        cell_w, cell_h = printable_width // cols, printable_height // rows
                        canvas = Image.new("RGB", (printable_width, printable_height), "white")
                        for k, pno in enumerate(sheet):
                            pg = pdf_doc.load_page(pno)
                            fit = min(cell_w * 0.94 / (pg.rect.width * dpi_scale_x),
                                      cell_h * 0.94 / (pg.rect.height * dpi_scale_y))
                            pm = pg.get_pixmap(matrix=fitz.Matrix(dpi_scale_x * fit, dpi_scale_y * fit), alpha=False)
                            tile = Image.frombytes("RGB", [pm.width, pm.height], pm.samples)
                            cx, cy = (k % cols) * cell_w, (k // cols) * cell_h
                            canvas.paste(tile, (cx + (cell_w - pm.width) // 2, cy + (cell_h - pm.height) // 2))
                        ImageWin.Dib(canvas).draw(hdc.GetHandleOutput(), (0, 0, printable_width, printable_height))
                        hdc.EndPage()
                        continue

                    page = pdf_doc.load_page(sheet[0])
                    logger.info(
                        "DC imageable area: %d × %d px  (page PDF rect: %.1f × %.1f pts)",
                        printable_width, printable_height,
                        page.rect.width, page.rect.height,
                    )

                    # --------------------------------------------------
                    # DPI-based scaling: render at native printer DPI
                    # (1 PDF pt = DPI/72 pixels).
                    # --------------------------------------------------
                    rendered_w = page.rect.width * dpi_scale_x
                    rendered_h = page.rect.height * dpi_scale_y

                    scale_w = printable_width / rendered_w if rendered_w > 0 else 1.0
                    scale_h = printable_height / rendered_h if rendered_h > 0 else 1.0

                    if scaling == "fit":
                        fit_scale = min(scale_w, scale_h)
                    elif scaling == "fill":
                        fit_scale = max(scale_w, scale_h)
                    elif scaling == "none":
                        fit_scale = 1.0
                    # If width matches printable area (within 5%) but height is much smaller
                    # (typical for thermal label printers where the driver form height is smaller
                    # than the actual label stock), DO NOT shrink width to fit height!
                    elif scale_w >= 0.95 and scale_h < 0.7:
                        logger.warning(
                            "Printer DC height (%d px) is significantly smaller than document height (%d px), "
                            "but width matches (%.1f%%). Preserving 1:1 scale to avoid shrunken label.",
                            printable_height, int(rendered_h), scale_w * 100.0,
                        )
                        fit_scale = min(1.0, scale_w)
                    elif rendered_w > printable_width or rendered_h > printable_height:
                        fit_scale = min(scale_w, scale_h)
                        logger.info("Page exceeds printable area — shrink-to-fit scale=%.4f", fit_scale)
                    else:
                        fit_scale = 1.0

                    final_scale_x = dpi_scale_x * fit_scale
                    final_scale_y = dpi_scale_y * fit_scale

                    matrix = fitz.Matrix(final_scale_x, final_scale_y)
                    pix = page.get_pixmap(matrix=matrix, alpha=False)

                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                    if scaling in ("fill", "none"):                # may be larger than the sheet: centre, let GDI clip
                        x_offset = (printable_width - pix.width) // 2
                        y_offset = (printable_height - pix.height) // 2
                    else:
                        x_offset = max(0, (printable_width - pix.width) // 2)
                        y_offset = max(0, (printable_height - pix.height) // 2)

                    dib = ImageWin.Dib(img)
                    dib.draw(
                        hdc.GetHandleOutput(),
                        (x_offset, y_offset, x_offset + pix.width, y_offset + pix.height)
                    )

                    hdc.EndPage()

            pdf_doc.close()
            hdc.EndDoc()
            hdc.DeleteDC()
            logger.info("Successfully spooled to printer.")
        finally:
            win32print.ClosePrinter(hprinter)
    except Exception:
        logger.exception("win32ui printing FAILED for '%s'", file_path)
        raise
    finally:
        pythoncom.CoUninitialize()



# ---------------------------------------------------------------------------
# IPP binary protocol helpers
# ---------------------------------------------------------------------------

def _encode_attribute(
    value_tag: int,
    name: str,
    value: bytes,
) -> bytes:
    """Encode a single IPP attribute into its binary wire format."""
    name_bytes = name.encode("ascii")
    return (
        struct.pack("!B", value_tag)
        + struct.pack("!H", len(name_bytes))
        + name_bytes
        + struct.pack("!H", len(value))
        + value
    )


def _encode_text_attribute(
    value_tag: int,
    name: str,
    text: str,
) -> bytes:
    """Convenience wrapper for text-valued attributes."""
    return _encode_attribute(value_tag, name, text.encode("utf-8"))


def _encode_integer_attribute(name: str, value: int) -> bytes:
    """Encode a 32-bit signed integer attribute."""
    return _encode_attribute(
        IPP_TAG_INTEGER, name, struct.pack("!i", value)
    )


def _encode_boolean_attribute(name: str, value: bool) -> bytes:
    """Encode a boolean attribute."""
    return _encode_attribute(
        IPP_TAG_BOOLEAN, name, struct.pack("!B", int(value))
    )


def _encode_enum_attribute(name: str, value: int) -> bytes:
    """Encode an enum (32-bit) attribute."""
    return _encode_attribute(
        IPP_TAG_ENUM, name, struct.pack("!i", value)
    )


def _encode_range_attribute(name: str, lower: int, upper: int) -> bytes:
    """Encode a rangeOfInteger attribute."""
    return _encode_attribute(
        IPP_TAG_RANGE, name, struct.pack("!ii", lower, upper)
    )


def _ipp_collection(name: str, members) -> bytes:
    """Encode a collection attribute. members = [(member_name, value_tag, value_bytes | nested_members)]."""
    def body(items) -> bytes:
        out = b""
        for mname, tag, val in items:
            out += struct.pack("!BH", IPP_TAG_MEMBER_ATTR_NAME, 0) + struct.pack("!H", len(mname)) + mname.encode()
            if tag == IPP_TAG_BEG_COLLECTION:
                out += struct.pack("!BHH", IPP_TAG_BEG_COLLECTION, 0, 0) + body(val) + struct.pack("!BHH", IPP_TAG_END_COLLECTION, 0, 0)
                out += struct.pack("!BHH", 0x4A, 0, 0) if False else b""
            else:
                out += struct.pack("!BH", tag, 0) + struct.pack("!H", len(val)) + val
        return out
    return (struct.pack("!B", IPP_TAG_BEG_COLLECTION) + struct.pack("!H", len(name)) + name.encode() + struct.pack("!H", 0)
            + body(members) + struct.pack("!BHH", IPP_TAG_END_COLLECTION, 0, 0))


def _encode_resolution_attribute(name: str, x: int, y: int, first: bool = True) -> bytes:
    return _encode_attribute(0x32, name if first else "", struct.pack("!iiB", x, y, 3))     # 3 = dots per inch


def _media_col_sized(w_mm: float, h_mm: float, borderless: bool = False) -> list:
    m = 0 if borderless else 300                                   # 3 mm printable margin (hundredths of mm)
    return [("media-bottom-margin", IPP_TAG_INTEGER, struct.pack("!i", m)),
            ("media-left-margin", IPP_TAG_INTEGER, struct.pack("!i", m)),
            ("media-right-margin", IPP_TAG_INTEGER, struct.pack("!i", m)),
            ("media-top-margin", IPP_TAG_INTEGER, struct.pack("!i", m)),
            ("media-size", IPP_TAG_BEG_COLLECTION, [
                ("x-dimension", IPP_TAG_INTEGER, struct.pack("!i", int(round(w_mm * 100)))),
                ("y-dimension", IPP_TAG_INTEGER, struct.pack("!i", int(round(h_mm * 100))))])]


def _media_col(media_key: str) -> list:
    w, h = IPP_MEDIA_SIZES.get(media_key, (210.0, 297.0))
    return [("media-size", IPP_TAG_BEG_COLLECTION, [
                ("x-dimension", IPP_TAG_INTEGER, struct.pack("!i", int(round(w * 100)))),
                ("y-dimension", IPP_TAG_INTEGER, struct.pack("!i", int(round(h * 100))))])]


def _encode_additional_value(value_tag: int, value: bytes) -> bytes:
    """
    Encode an *additional value* for a multi-valued attribute.

    Per RFC 8011 §3.1.4 the name-length is 0 and no name follows.
    """
    return (
        struct.pack("!B", value_tag)
        + struct.pack("!H", 0)     # zero-length name
        + struct.pack("!H", len(value))
        + value
    )


def build_ipp_response(
    request_id: int,
    status_code: int,
    extra_groups: bytes = b"",
    version_major: int = 2,
    version_minor: int = 0,
) -> bytes:
    """
    Assemble a minimal valid IPP response.

    Parameters
    ----------
    request_id : int
        Must echo the ``request-id`` from the client's request.
    status_code : int
        IPP status-code (``0x0000`` = successful-ok).
    extra_groups : bytes
        Pre-encoded attribute groups to append between the mandatory
        operation-attributes group and the end-of-attributes tag.
    version_major : int
        IPP major version — should echo the client's request version.
    version_minor : int
        IPP minor version — should echo the client's request version.
    """
    # ---- header (8 bytes) ----
    # Per RFC 8011 §4.1.8, the response version MUST match the request.
    header = struct.pack(
        "!BBHi",
        version_major,
        version_minor,
        status_code,
        request_id,
    )

    # ---- mandatory operation attributes group ----
    body = struct.pack("!B", IPP_TAG_OPERATION)
    body += _encode_text_attribute(
        IPP_TAG_CHARSET, "attributes-charset", "utf-8"
    )
    body += _encode_text_attribute(
        IPP_TAG_LANGUAGE, "attributes-natural-language", "en-us"
    )

    # ---- optional extra attribute groups ----
    body += extra_groups

    # ---- end of attributes ----
    body += struct.pack("!B", IPP_TAG_END)

    return header + body


def parse_ipp_request(raw: bytes) -> Tuple[int, int, int, int, int]:
    """
    Parse the 8-byte IPP header.

    Returns ``(version_major, version_minor, operation_id, request_id)``.
    The combined version is also returned for back-compat logging.
    Returns a 5-tuple: ``(version_combined, op_id, req_id, ver_major, ver_minor)``.
    """
    if len(raw) < 8:
        raise ValueError("IPP payload too short (< 8 bytes)")
    ver_major, ver_minor, op_id, req_id = struct.unpack("!BBHi", raw[:8])
    return (ver_major << 8 | ver_minor), op_id, req_id, ver_major, ver_minor


def extract_document_data(raw: bytes) -> bytes:
    """
    Locate the document payload inside a ``Print-Job`` request.

    The IPP spec says document data follows the *end-of-attributes-tag*
    (``0x03``).  We search for the tag that genuinely marks the boundary
    by walking through attribute groups properly.
    """
    # Walk through the IPP attributes to find the real end-of-attributes tag.
    # The header is 8 bytes; attributes start at offset 8.
    idx = 8
    while idx < len(raw):
        tag = raw[idx]
        idx += 1

        # Delimiter tags (0x00-0x05) — they occupy a single byte.
        if tag <= 0x05:
            if tag == IPP_TAG_END:
                # Everything after this byte is document data.
                return raw[idx:]
            # Other delimiter tags (operation, job, printer, …) — continue.
            continue

        # Value tag — followed by name-length(2) + name + value-length(2) + value
        if idx + 2 > len(raw):
            break
        name_len = struct.unpack("!H", raw[idx:idx + 2])[0]
        idx += 2 + name_len
        if idx + 2 > len(raw):
            break
        value_len = struct.unpack("!H", raw[idx:idx + 2])[0]
        idx += 2 + value_len

    # Structured walk failed to find end-of-attributes tag.
    # Do NOT brute-force scan for 0x03 — it can appear inside attribute
    # values (URIs, text) and would corrupt the document boundary.
    logger.warning("Structured IPP parse did not find end-of-attributes tag")
    return b""


# ---------------------------------------------------------------------------
# IPP media-size keyword → physical dimensions (width_mm, height_mm)
# ---------------------------------------------------------------------------
# Keys are the standard IPP "media" keyword values defined in PWG 5101.1.
# Values are (width_mm, height_mm).
IPP_MEDIA_SIZES: dict[str, Tuple[float, float]] = {
    # ISO A-series
    "iso_a3_297x420mm":    (297.0, 420.0),
    "iso_a4_210x297mm":    (210.0, 297.0),
    "iso_a5_148x210mm":    (148.0, 210.0),
    "iso_a6_105x148mm":    (105.0, 148.0),
    "iso_a7_74x105mm":     (74.0,  105.0),
    "iso_a8_52x74mm":      (52.0,  74.0),
    # ISO B-series
    "iso_b5_176x250mm":    (176.0, 250.0),
    "iso_b6_125x176mm":    (125.0, 176.0),
    # ISO C-series (envelopes)
    "iso_c5_162x229mm":    (162.0, 229.0),
    "iso_c6_114x162mm":    (114.0, 162.0),
    # ISO DL envelope
    "iso_dl_110x220mm":    (110.0, 220.0),
    # North American sizes
    "na_letter_8.5x11in":  (215.9, 279.4),
    "na_legal_8.5x14in":   (215.9, 355.6),
    "na_executive_7.25x10.5in": (184.15, 266.7),
    "na_invoice_5.5x8.5in":    (139.7, 215.9),
    # Common label / receipt sizes
    "na_index-4x6_4x6in": (101.6, 152.4),
    "om_small-photo_100x150mm": (100.0, 150.0),
    "om_100x150mm_100x150mm":   (100.0, 150.0),
    "custom_4x6in_4x6in":      (101.6, 152.4),
}


def extract_ipp_job_attributes(raw: bytes) -> dict[str, str]:
    """
    Walk the IPP attribute groups in *raw* and return a dict of
    interesting job-template attributes (string-valued).

    Extracts: ``media``, ``media-type``, ``copies``, ``sides``,
    ``x-dimension``, ``y-dimension``, collection members (e.g. from ``media-col``),
    and falls back to scanning pre-document header bytes for known media keywords.
    """
    attrs: dict[str, str] = {}
    idx = 8  # skip the 8-byte IPP header

    # Value-tag ranges that encode text / keyword / name / uri etc.
    _TEXT_TAGS = {
        IPP_TAG_TEXT, IPP_TAG_NAME, IPP_TAG_KEYWORD,
        IPP_TAG_URI, IPP_TAG_URISCHEME, IPP_TAG_CHARSET,
        IPP_TAG_LANGUAGE, IPP_TAG_MIMETYPE,
    }

    current_member_name = ""
    last_attr_name = ""

    while idx < len(raw):
        tag = raw[idx]
        idx += 1

        # Delimiter tags (0x00-0x05)
        if tag <= 0x05:
            if tag == IPP_TAG_END:
                break
            continue

        # Value tag — parse name + value
        if idx + 2 > len(raw):
            break
        name_len = struct.unpack("!H", raw[idx:idx + 2])[0]
        idx += 2
        if idx + name_len > len(raw):
            break
        attr_name = raw[idx:idx + name_len].decode("ascii", errors="replace") if name_len else ""
        idx += name_len

        if idx + 2 > len(raw):
            break
        value_len = struct.unpack("!H", raw[idx:idx + 2])[0]
        idx += 2
        if idx + value_len > len(raw):
            break
        attr_value_raw = raw[idx:idx + value_len]
        idx += value_len

        # In IPP collections, member attribute names have tag 0x4A and name_len == 0
        if tag == IPP_TAG_MEMBER_ATTR_NAME:
            current_member_name = attr_value_raw.decode("ascii", errors="replace")
            continue

        if attr_name:
            last_attr_name = attr_name
        if tag == IPP_TAG_RANGE and value_len == 8:                  # e.g. page-ranges (1setOf rangeOfInteger)
            lo, hi = struct.unpack("!ii", attr_value_raw)
            key = attr_name or last_attr_name
            if key:
                attrs[key] = (attrs.get(key, "") + "," if attr_name == "" and key in attrs else "") + f"{lo}-{hi}"
            continue

        # Effective attribute name (regular or collection member)
        effective_name = attr_name or current_member_name

        if effective_name:
            if tag in _TEXT_TAGS:
                val = attr_value_raw.decode("utf-8", errors="replace")
                if effective_name in ("media-size-name", "media"):
                    attrs["media"] = val
                else:
                    attrs[effective_name] = val
            elif tag in (IPP_TAG_INTEGER, IPP_TAG_ENUM) and value_len == 4:
                val_int = struct.unpack("!i", attr_value_raw)[0]
                attrs[effective_name] = str(val_int)

    # Fallback: If 'media' was not parsed, scan the IPP header bytes
    # (before end-of-attributes) for any known keyword in IPP_MEDIA_SIZES
    if "media" not in attrs:
        header_bytes = raw[:idx]
        for keyword in sorted(IPP_MEDIA_SIZES.keys(), key=len, reverse=True):
            if keyword.encode("ascii") in header_bytes:
                logger.info("Found media keyword '%s' via header byte scan", keyword)
                attrs["media"] = keyword
                break

    logger.info("Parsed IPP job attributes: %s", attrs)
    return attrs


# ---------------------------------------------------------------------------
# Build rich Get-Printer-Attributes response
# ---------------------------------------------------------------------------

_media_cache: dict = {}
_FALLBACK_MEDIA = ["iso_a4_210x297mm", "na_letter_8.5x11in", "iso_a5_148x210mm", "iso_a6_105x148mm", "iso_a7_74x105mm",
                   "iso_a8_52x74mm", "na_legal_8.5x14in", "na_index-4x6_4x6in", "om_small-photo_100x150mm"]


def _icon_kind_flags(printer_name: str, cfg: Optional[dict], has_scanner: bool) -> tuple:
    caps = driver_caps.peek(printer_name)
    is_label = bool(caps and any(b["keyword"] in ("continuous-roll", "roll") for b in caps.input_bins()))
    return icons.kind_for(cfg, has_scanner, printer_is_color(printer_name, cfg), is_label)


def is_mobile_client(user_agent: str) -> bool:
    """Phones/tablets (Android print service, Mopria, iOS ...) can't pick borderless as an option, so they must not be handed
    zero-margin variants of ordinary paper; Macs and Windows PCs can (and ask for it explicitly)."""
    ua = (user_agent or "").lower()
    return any(k in ua for k in ("android", "iphone", "ipad", "ios", "mopria", "wprint"))


A4_BORDERLESS = "iso_a4-borderless_210x297mm"          # "A4 borderless" offered to phones as a paper size of its own


def is_photo_size(w_mm: float, h_mm: float) -> bool:
    """Borderless is only offered/used for photo-type paper (up to A5 / 5x7 in and around).  Offering a zero-margin A4 made phones
    pick it by default, and the bridge then enlarged ordinary PDFs to fill the page instead of printing them at actual size."""
    short, long_ = sorted((w_mm, h_mm))
    return short <= 130.0 and long_ <= 210.5


def size_from_keyword(keyword: str) -> Optional[Tuple[float, float]]:
    """(width, height) in mm from a PWG self-describing media name such as om_100x100mm_100x100mm or na_index-4x6_4x6in;
    None if the name does not end in <W>x<H>mm|in.  Lets any label size work without a table entry."""
    m = re.search(r"_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(mm|in)$", keyword or "")
    if not m:
        return None
    w, h = float(m.group(1)), float(m.group(2))
    k = 25.4 if m.group(3) == "in" else 1.0
    return round(w * k, 1), round(h * k, 1)


def supported_media(printer_name: str, cfg: Optional[dict], default_media: str) -> list:
    """Paper sizes to advertise: config "media": [...] if given, else the printer's own Windows forms
    (mapped to IPP names, unknown ones as custom_<name>_<w>x<h>mm), else a generic list.  Cached 5 minutes."""
    if cfg and isinstance(cfg.get("media"), list) and cfg["media"]:
        found = [str(m) for m in cfg["media"]]
        for m in found:                                    # self-describing names (om_100x100mm_100x100mm) need no table entry
            if m not in IPP_MEDIA_SIZES and size_from_keyword(m):
                IPP_MEDIA_SIZES[m] = size_from_keyword(m)
    else:
        hit = _media_cache.get(printer_name)
        if hit and time.time() - hit[0] < 300:
            found = list(hit[1])
        else:
            found = []
            try:
                papers = win32print.DeviceCapabilities(printer_name, "", 2)      # DC_PAPERS
                sizes = win32print.DeviceCapabilities(printer_name, "", 3)       # DC_PAPERSIZE (0.1 mm)
                names = win32print.DeviceCapabilities(printer_name, "", 16)      # DC_PAPERNAMES
                known, custom = [], []
                for sz, nm in zip(sizes, names):
                    w, h = sorted((sz["x"] / 10.0, sz["y"] / 10.0))
                    if w < 20 or h > 1000:
                        continue
                    kw = next((k for k, (mw, mh) in IPP_MEDIA_SIZES.items()
                               if abs(min(mw, mh) - w) < 1.5 and abs(max(mw, mh) - h) < 1.5), None)
                    if kw:
                        if kw not in known:
                            known.append(kw)
                    else:
                        ck = f"custom_{_slug(str(nm).strip())[:30]}_{w:g}x{h:g}mm"
                        IPP_MEDIA_SIZES[ck] = (w, h)
                        if ck not in custom:
                            custom.append(ck)
                found = (known + custom)[:24]
            except Exception:  # noqa: BLE001
                logger.debug("could not read the printer's paper forms", exc_info=True)
            if found:
                _media_cache[printer_name] = (time.time(), tuple(found))
    if not found:
        found = list(_FALLBACK_MEDIA)
    out = [default_media] + [m for m in found if m != default_media]
    return out


def _build_printer_attributes(
    printer_name: str,
    host_ip: str,
    cfg: Optional[dict] = None,
    url_prefix: str = "",
    mobile: bool = False,
) -> bytes:
    """
    Return the pre-encoded *printer-attributes* group that iOS / Android
    require in order to accept the printer as AirPrint-compatible.
    """
    hostname = socket.gethostname()
    display_name = get_display_name(printer_name, cfg)
    printer_uri = f"ipp://{host_ip}:{IPP_PORT}{url_prefix}/ipp/print"
    attrs = struct.pack("!B", IPP_TAG_PRINTER)

    # --- Identity ---
    attrs += _encode_text_attribute(IPP_TAG_URI, "printer-uri-supported", printer_uri)
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "uri-security-supported", "none")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "uri-authentication-supported", "none")
    attrs += _encode_text_attribute(IPP_TAG_NAME, "printer-name", printer_name)
    attrs += _encode_text_attribute(IPP_TAG_TEXT, "printer-info", display_name)
    attrs += _encode_text_attribute(IPP_TAG_TEXT, "printer-location", str((cfg or {}).get("location") or f"PC: {hostname}"))
    attrs += _encode_text_attribute(IPP_TAG_TEXT, "printer-make-and-model", str((cfg or {}).get("model") or display_name))

    # --- AirPrint feature declaration (CRITICAL for iOS) ---
    # iOS uses this attribute to confirm AirPrint capability.
    # Without "airprint-1.8", iOS silently rejects the printer.
    attrs += _encode_text_attribute(
        IPP_TAG_KEYWORD, "ipp-features-supported", "airprint-1.8"
    )

    # --- State ---
    # 3 = idle, 4 = processing, 5 = stopped
    pstat = job_tracking.printer_status(printer_name)
    attrs += _encode_enum_attribute("printer-state", pstat["state"])
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "printer-state-reasons", pstat["reasons"][0])
    for extra in pstat["reasons"][1:]:
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, extra.encode("ascii"))

    # --- Capabilities ---
    # iOS AirPrint and Android IPP Everywhere / Mopria supported formats list.
    attrs += _encode_text_attribute(IPP_TAG_MIMETYPE, "document-format-supported", "application/pdf")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"image/urf")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"image/jpeg")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"image/png")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"image/pwg-raster")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"application/octet-stream")

    attrs += _encode_text_attribute(IPP_TAG_MIMETYPE, "document-format-default", "application/pdf")

    # Operations supported (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs)
    attrs += _encode_enum_attribute("operations-supported", IPP_OP_PRINT_JOB)
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_VALIDATE_JOB))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_GET_PRINTER_ATTRIBUTES))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_GET_JOBS))

    # Charset & language
    attrs += _encode_text_attribute(IPP_TAG_CHARSET, "charset-configured", "utf-8")
    attrs += _encode_text_attribute(IPP_TAG_CHARSET, "charset-supported", "utf-8")
    attrs += _encode_text_attribute(IPP_TAG_LANGUAGE, "natural-language-configured", "en-us")
    attrs += _encode_text_attribute(IPP_TAG_LANGUAGE, "generated-natural-language-supported", "en-us")

    # Color support — MUST be boolean per IPP spec; iOS rejects keyword encoding.
    is_color = printer_is_color(printer_name, cfg)
    attrs += _encode_boolean_attribute("color-supported", is_color)
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "print-color-mode-default", "auto" if is_color else "monochrome")
    if is_color:
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "print-color-mode-supported", "auto")
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"color")
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"monochrome")
    else:
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "print-color-mode-supported", "monochrome")

    # Pages-per-minute (informational)
    attrs += _encode_integer_attribute("pages-per-minute", int((cfg or {}).get("ppm") or 10))

    # Detect the printer's actual active paper form in Windows DEVMODE
    default_media = "iso_a4_210x297mm"
    try:
        hprinter = win32print.OpenPrinter(printer_name)
        try:
            props = win32print.GetPrinter(hprinter, 2)
            devmode = props["pDevMode"]
            if devmode.PaperSize == win32con.DMPAPER_A4:
                default_media = "iso_a4_210x297mm"
            elif devmode.PaperSize == win32con.DMPAPER_A5:
                default_media = "iso_a5_148x210mm"
            elif devmode.PaperSize == win32con.DMPAPER_A6:
                default_media = "iso_a6_105x148mm"
            elif devmode.PaperSize == win32con.DMPAPER_LETTER:
                default_media = "na_letter_8.5x11in"
            elif devmode.PaperSize == win32con.DMPAPER_LEGAL:
                default_media = "na_legal_8.5x14in"
            elif devmode.PaperWidth > 0 and devmode.PaperLength > 0:
                pw_mm = devmode.PaperWidth / 10.0
                ph_mm = devmode.PaperLength / 10.0
                for k, (mw, mh) in IPP_MEDIA_SIZES.items():
                    if (abs(mw - pw_mm) < 3.0 and abs(mh - ph_mm) < 3.0) or \
                       (abs(mw - ph_mm) < 3.0 and abs(mh - pw_mm) < 3.0):
                        default_media = k
                        break
        finally:
            win32print.ClosePrinter(hprinter)
    except Exception:
        pass
    configured_default = get_default_paper(cfg)
    if configured_default:
        default_media = configured_default[0]                  # phones/PCs then offer it first
    logger.info("Active printer default media detected: %s", default_media)

    # Media & page size — advertise sizes with default_media first
    all_media_sizes = supported_media(printer_name, cfg, default_media)

    # Remove duplicates while preserving order
    seen_media = set()
    unique_media = []
    for m in all_media_sizes:
        if m not in seen_media:
            seen_media.add(m)
            unique_media.append(m)

    supported_list = list(unique_media)
    if mobile and driver_caps.peek(printer_name) and driver_caps.peek(printer_name).borderless_supported() \
            and "iso_a4_210x297mm" in unique_media and A4_BORDERLESS not in supported_list:
        IPP_MEDIA_SIZES[A4_BORDERLESS] = (210.0, 297.0)
        supported_list.insert(supported_list.index("iso_a4_210x297mm") + 1, A4_BORDERLESS)      # phones have no borderless switch
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-default", unique_media[0])
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-supported", supported_list[0])
    for m in supported_list[1:]:
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, m.encode("ascii"))

    # media-ready — iOS 16+ requires this to show the printer.
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-ready", unique_media[0])
    for m in unique_media[1:]:
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, m.encode("ascii"))


    # media-col-supported — iOS 16+ checks for this collection attribute.
    caps = driver_caps.peek(printer_name)
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-col-supported", "media-size")
    for kw in ("media-type", "media-source", "media-bottom-margin", "media-left-margin", "media-right-margin", "media-top-margin"):
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, kw.encode())
    attrs += _ipp_collection("media-col-default", _media_col(unique_media[0]))
    attrs += _ipp_collection("media-col-ready", _media_col(unique_media[0]))

    # Every size the printer takes, plus a zero-margin (borderless) twin when the driver offers borderless.
    first = True
    for m in unique_media[:40]:
        wh = IPP_MEDIA_SIZES.get(m)
        if not wh:
            continue
        # zero-margin (borderless) twin: photo sizes for everybody; every other size (A4 ...) only for Macs/PCs, never phones
        twin = bool(caps and caps.borderless_supported() and (is_photo_size(*wh) or not mobile))
        for borderless in ((False, True) if twin else (False,)):
            attrs += _ipp_collection("media-col-database" if first else "", _media_col_sized(wh[0], wh[1], borderless))
            first = False

    # Media types straight from the driver (plain, glossy, matte, luster, envelope ... whatever this printer has)
    if caps and caps.media_types():
        types = caps.media_types()
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-type-supported", types[0]["keyword"])
        for t in types[1:]:
            attrs += _encode_additional_value(IPP_TAG_KEYWORD, t["keyword"].encode())
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-type-default", types[0]["keyword"])

    # Sides: two-sided only when the printer really flips the page itself (manual duplex is not offered)
    sides = list((caps.duplex_options() if caps else {}) or ["one-sided"])
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "sides-default", "one-sided")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "sides-supported", sides[0])
    for sd in sides[1:]:
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, sd.encode())

    # Paper trays from the driver
    bins = caps.input_bins() if caps else []
    if len(bins) > 1:
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-source-supported", bins[0]["keyword"])
        for b in bins[1:]:
            attrs += _encode_additional_value(IPP_TAG_KEYWORD, b["keyword"].encode())
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-source-default", bins[0]["keyword"])

    # Output order / collation (done by the bridge)
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "page-delivery-supported", "same-order")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"reverse-order")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "page-delivery-default", "same-order")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "sheet-collate-supported", "collated")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"uncollated")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "sheet-collate-default", "collated")

    # Copies
    attrs += _encode_range_attribute("copies-supported", 1, 99)
    attrs += _encode_integer_attribute("copies-default", 1)

    # IPP versions
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "ipp-versions-supported", "1.1")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"2.0")

    # Multiple document handling
    attrs += _encode_text_attribute(
        IPP_TAG_KEYWORD,
        "multiple-document-jobs-supported",
        "false",
    )

    # Ink / toner levels (only when the printer can be asked over the network - see supplies.py)
    sup = supplies.get(cfg) if (cfg or {}).get("supplies_ip") else {"items": []}
    if sup["items"]:
        its = sup["items"]
        attrs += _encode_text_attribute(IPP_TAG_NAME, "marker-names", its[0]["name"])
        for it in its[1:]:
            attrs += _encode_additional_value(IPP_TAG_NAME, it["name"].encode())
        attrs += _encode_integer_attribute("marker-levels", int(its[0]["level"]))
        for it in its[1:]:
            attrs += _encode_additional_value(IPP_TAG_INTEGER, struct.pack("!i", int(it["level"])))
        attrs += _encode_text_attribute(IPP_TAG_NAME, "marker-colors", its[0]["color"] or "none")
        for it in its[1:]:
            attrs += _encode_additional_value(IPP_TAG_NAME, (it["color"] or "none").encode())
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "marker-types", its[0]["type"] or "ink-cartridge")
        for it in its[1:]:
            attrs += _encode_additional_value(IPP_TAG_KEYWORD, (it["type"] or "ink-cartridge").encode())

    # Accepting jobs
    attrs += _encode_boolean_attribute("printer-is-accepting-jobs", True)

    # Number of queued jobs
    attrs += _encode_integer_attribute("queued-job-count", pstat["queued"])

    # PDL override
    attrs += _encode_text_attribute(
        IPP_TAG_KEYWORD, "pdl-override-supported", "not-attempted"
    )

    # Compression
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "compression-supported", "none")

    # Print quality (draft / normal / high) - only the levels this driver really has
    levels = sorted(caps.quality_options()) if caps and caps.quality_options() else [3, 4, 5]
    attrs += _encode_enum_attribute("print-quality-default", 4)  # 4 = normal
    attrs += _encode_enum_attribute("print-quality-supported", levels[0])
    for lv in levels[1:]:
        attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", lv))

    # Options the bridge applies itself, whatever the driver: page ranges, pages per sheet, scaling
    attrs += _encode_boolean_attribute("page-ranges-supported", True)
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "print-scaling-default", "auto")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "print-scaling-supported", "auto")
    for kw in ("fit", "fill", "none"):
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, kw.encode())
    attrs += _encode_integer_attribute("number-up-default", 1)
    attrs += _encode_integer_attribute("number-up-supported", 1)
    for n in (2, 4, 6, 9, 16):
        attrs += _encode_additional_value(IPP_TAG_INTEGER, struct.pack("!i", n))
    res = caps.resolutions() if caps else []
    if res:
        attrs += _encode_resolution_attribute("printer-resolution-supported", *res[0])
        for r in res[1:]:
            attrs += _encode_resolution_attribute("printer-resolution-supported", *r, first=False)
        attrs += _encode_resolution_attribute("printer-resolution-default", *res[0])
    jc = ["copies", "media", "media-col", "print-color-mode", "print-quality", "page-ranges", "number-up", "print-scaling",
          "sides", "page-delivery", "sheet-collate"]
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "job-creation-attributes-supported", jc[0])
    for kw in jc[1:]:
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, kw.encode())

    # Printer UUID (deterministic from printer name + hostname, RFC 4122 UUID5)
    # MUST strictly match the UUID in the mDNS TXT record so Android BIPS does not reject it.
    printer_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{printer_name}@{hostname}"))
    attrs += _encode_text_attribute(
        IPP_TAG_URI, "printer-uuid", f"urn:uuid:{printer_uuid}"
    )

    # printer-more-info — iOS checks for this URI
    attrs += _encode_text_attribute(
        IPP_TAG_URI, "printer-more-info",
        f"http://{host_ip}:{IPP_PORT}/",
    )

    # Printer pictures (PNG 48/128/512) - phones show them on the printer's page
    pid = _slug(display_name)
    icon_uris = [f"http://{host_ip}:{IPP_PORT}{url_prefix}/icons/{pid}-{sz}.png" for sz in (48, 128, 512)]
    attrs += _encode_text_attribute(IPP_TAG_URI, "printer-icons", icon_uris[0])
    for u in icon_uris[1:]:
        attrs += _encode_additional_value(IPP_TAG_URI, u.encode())

    # URF (Apple Raster) capabilities - iOS requires this attribute.  Built from what the printer really is:
    # W8 = grayscale, SRGB24 = colour (only if the printer is colour), RSx-y = resolutions, DM1 = two-sided (only if it duplexes).
    urf = printer_traits(printer_name, cfg)["urf"].split(",")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "urf-supported", urf[0])
    for u in urf[1:]:
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, u.encode())

    # AirPrint-specific: printer-type flags
    # Bit 0 = local, Bit 2 = can print — 0x05 covers the basics
    attrs += _encode_integer_attribute("printer-type", 0x00000005)

    return attrs


# ---------------------------------------------------------------------------
# IPP-aware HTTP request handler
# ---------------------------------------------------------------------------

class PrinterCtx:
    """Everything that belongs to ONE shared printer (its Windows queue, config, scanner and WSD device)."""

    def __init__(self, pid: str, printer_name: str, cfg: dict, ip: str = "") -> None:
        self.id, self.printer_name, self.cfg = pid, printer_name, cfg
        self.ip = ip                     # the address this printer is announced at ("ip" in config, else the PC's own)
        self.escl = None
        self.wsd = None


class IPPRequestHandler(BaseHTTPRequestHandler):
    """
    Minimal HTTP handler that speaks enough IPP to satisfy AirPrint and
    Android's built-in IPP client.
    """

    host_ip: str = "127.0.0.1"
    contexts: dict = {}               # printer id -> PrinterCtx
    default_ctx = None                # the first printer: also answers the old un-prefixed URLs
    ctx = None
    url_prefix = ""

    @property
    def printer_name(self) -> str:
        return self.ctx.printer_name

    @property
    def escl(self):
        return self.ctx.escl

    @property
    def wsd(self):
        return self.ctx.wsd

    def _route(self) -> None:
        """Pick the printer from a /<id>/... URL prefix (stripped from self.path); no prefix = first printer."""
        self.ctx, self.url_prefix = self.default_ctx, ""
        for pid, c in self.contexts.items():
            if self.path == f"/{pid}" or self.path.startswith(f"/{pid}/"):
                self.ctx, self.url_prefix = c, f"/{pid}"
                self.path = self.path[len(self.url_prefix):] or "/"
                break

    # Silence the default stderr logging — we log to file.
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401
        if str(args[0] if args else "").startswith("GET /api/"):
            return                       # the web page polls every few seconds - keep the log readable
        logger.info("HTTP  %s  %s", self.address_string(), fmt % args)

    # Support HTTP/1.1 so that we can handle Expect: 100-continue and Chunked transfer
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ #
    # POST — the only verb iOS / Android use for IPP                      #
    # ------------------------------------------------------------------ #
    def do_POST(self) -> None:  # noqa: N802
        self._route()
        logger.info("Headers received from %s:\n%s", self.client_address[0], self.headers)
        
        # Handle chunked transfer encoding (common in iOS AirPrint for large jobs)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            logger.info("Client is using chunked transfer encoding!")
            raw = bytearray()
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    continue
                chunk_size = int(line, 16)
                if chunk_size == 0:
                    self.rfile.readline()  # Read trailing \r\n
                    break
                raw.extend(self.rfile.read(chunk_size))
                self.rfile.readline()  # Read trailing \r\n
            raw = bytes(raw)
            content_length = len(raw)
        else:
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length)

        logger.info(
            "POST %s  Content-Length=%d  from %s",
            self.path,
            content_length,
            self.client_address[0],
        )

        if self.path.startswith("/eSCL"):
            self._handle_escl("POST", raw)
            return
        if webui.is_ui_path(self.path):
            webui.handle(self, "POST", raw)
            return
        if self.path.startswith("/WebServices/") and self.wsd is not None:
            code, ctype, out, hdrs = self.wsd.handle(self.path, raw, self.headers.get("Content-Type", ""))
            self._send_plain(code, out, ctype, hdrs)
            return

        if len(raw) < 8:
            self._send_ipp_error(0, IPP_STATUS_BAD_REQUEST)
            return

        try:
            _version, op_id, req_id, ver_maj, ver_min = parse_ipp_request(raw)
        except ValueError as exc:
            logger.warning("Malformed IPP header: %s", exc)
            self._send_ipp_error(0, IPP_STATUS_BAD_REQUEST)
            return

        logger.info(
            "IPP %d.%d  operation=0x%04X  request-id=%d",
            ver_maj, ver_min, op_id, req_id,
        )

        if op_id == IPP_OP_PRINT_JOB:
            self._handle_print_job(raw, req_id, ver_maj, ver_min)
        elif op_id == IPP_OP_VALIDATE_JOB:
            self._handle_validate_job(req_id, ver_maj, ver_min)
        elif op_id == IPP_OP_GET_PRINTER_ATTRIBUTES:
            self._handle_get_printer_attributes(req_id, ver_maj, ver_min)
        elif op_id == IPP_OP_GET_JOBS:
            self._handle_get_jobs(req_id, ver_maj, ver_min, raw)
        elif op_id == IPP_OP_GET_JOB_ATTRIBUTES:
            self._handle_get_job_attributes(req_id, ver_maj, ver_min, raw)
        elif op_id == IPP_OP_CANCEL_JOB:
            self._handle_cancel_job(req_id, ver_maj, ver_min, raw)
        else:
            logger.warning("Unsupported IPP operation 0x%04X", op_id)
            self._send_ipp_error(req_id, IPP_STATUS_BAD_REQUEST)

    # ------------------------------------------------------------------ #
    # GET — some clients probe the root or /ipp/print via GET             #
    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        self._route()
        if not self.path.startswith("/api/"):
            logger.info(
                "GET %s  from %s", self.path, self.client_address[0]
            )
        if self.path.startswith("/eSCL"):
            self._handle_escl("GET", b"")
            return
        if self.path.startswith("/icons/"):
            m = re.match(r"^/icons/(.+)-(48|128|512)\.png$", self.path)
            ctx = self.contexts.get(m.group(1)) if m else None
            ctx = ctx or self.ctx
            if not m or ctx is None:
                self._send_plain(404, b"")
                return
            size, data = int(m.group(2)), None
            if ctx.cfg.get("icon_file"):
                data = icons.file_icon(str(ctx.cfg["icon_file"]), size)
            if data is None:
                data = icons.make_icon(_icon_kind_flags(ctx.printer_name, ctx.cfg, ctx.escl is not None), size,
                                       scanner=ctx.escl is not None, color=printer_is_color(ctx.printer_name, ctx.cfg))
            self._send_plain(200, data, "image/png", {"Cache-Control": "max-age=3600"})
            return
        if webui.is_ui_path(self.path):
            webui.handle(self, "GET")
            return
        # Return a simple human-readable status page.
        safe_name = html.escape(self.printer_name)
        body = (
            "<html><body>"
            "<h1>AirPrint Bridge</h1>"
            f"<p>Printer: <strong>{safe_name}</strong></p>"
            f"<p>IPP endpoint: <code>POST {self.url_prefix}/ipp/print</code></p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:  # noqa: N802
        self._route()
        logger.info("DELETE %s  from %s", self.path, self.client_address[0])
        if self.path.startswith("/eSCL"):
            self._handle_escl("DELETE", b"")
        else:
            self._send_plain(404, b"")

    def _send_plain(self, code: int, body: bytes, ctype: str = "text/plain",
                    headers: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle_escl(self, method: str, body: bytes) -> None:
        """eSCL (AirScan) endpoints - see escl_scanner.py."""
        scanner = self.escl
        if scanner is None:
            self._send_plain(404, b"scanning not enabled")
            return
        parts = [p for p in self.path.split("?")[0].split("/") if p]   # ['eSCL', ...]
        try:
            if method == "GET" and parts[1:] == ["ScannerCapabilities"]:
                self._send_plain(200, scanner.capabilities_xml(), "text/xml")
            elif method == "GET" and parts[1:] == ["ScannerStatus"]:
                self._send_plain(200, scanner.status_xml(), "text/xml")
            elif method == "POST" and parts[1:] == ["ScanJobs"]:
                job_id = scanner.create_job(body)
                host = self.headers.get("Host") or f"{self.ctx.ip or self.host_ip}:{IPP_PORT}"
                self._send_plain(201, b"", headers={
                    "Location": f"http://{host}{self.url_prefix}/eSCL/ScanJobs/{job_id}"})
            elif method == "GET" and len(parts) == 4 and parts[1] == "ScanJobs" and parts[3] == "NextDocument":
                doc = scanner.next_document(parts[2])
                if doc is None:
                    err = scanner.job_error(parts[2])
                    self._send_plain(503 if err else 404, err.encode("utf-8", "replace"))
                else:
                    self._send_plain(200, doc[0], doc[1])
            elif method == "DELETE" and len(parts) == 3 and parts[1] == "ScanJobs":
                scanner.delete_job(parts[2])
                self._send_plain(200, b"")
            else:
                self._send_plain(404, b"")
        except Exception:  # noqa: BLE001
            logger.exception("eSCL request failed: %s %s", method, self.path)
            self._send_plain(500, b"")

    # ------------------------------------------------------------------ #
    # IPP operation handlers                                              #
    # ------------------------------------------------------------------ #

    def _handle_print_job(
        self, raw: bytes, req_id: int, ver_maj: int, ver_min: int,
    ) -> None:
        """Extract the document payload and spool it."""
        # ---- Parse IPP job attributes (media, copies, etc.) ----
        job_attrs_parsed = extract_ipp_job_attributes(raw)
        if is_mobile_client(self.headers.get("User-Agent", "")):
            job_attrs_parsed["x-client-mobile"] = "1"
        media_keyword = re.sub(r"[._-]borderless$", "", job_attrs_parsed.get("media", ""), flags=re.I)
        media_size_mm: Optional[Tuple[float, float]] = None
        if media_keyword:
            media_size_mm = IPP_MEDIA_SIZES.get(media_keyword) or size_from_keyword(media_keyword)
            if media_size_mm:
                logger.info(
                    "IPP media='%s' → %.1f × %.1f mm",
                    media_keyword, media_size_mm[0], media_size_mm[1],
                )
            else:
                logger.warning(
                    "IPP media='%s' not found in lookup table",
                    media_keyword,
                )
        elif "x-dimension" in job_attrs_parsed and "y-dimension" in job_attrs_parsed:
            try:
                x_dim = int(job_attrs_parsed["x-dimension"]) / 100.0
                y_dim = int(job_attrs_parsed["y-dimension"]) / 100.0
                if x_dim > 0 and y_dim > 0:
                    media_size_mm = (x_dim, y_dim)
                    logger.info(
                        "Parsed media-col dimensions: %.1f × %.1f mm",
                        x_dim, y_dim,
                    )
            except (ValueError, TypeError):
                pass

        sender_chose = bool(media_keyword) or ("x-dimension" in job_attrs_parsed and "y-dimension" in job_attrs_parsed)
        media_size_mm, default_used = choose_media_size(media_size_mm, sender_chose, self.ctx.cfg)
        if default_used:
            logger.info("Sender did not choose a paper size - using default_paper=%s (%.0f x %.0f mm)",
                        default_used, media_size_mm[0], media_size_mm[1])

        doc_data = extract_document_data(raw)
        if not doc_data:
            logger.error("No document data found in Print-Job payload")
            self._send_ipp_error(req_id, IPP_STATUS_BAD_REQUEST)
            return

        ext, mime = detect_file_type(doc_data)
        logger.info(
            "Document extracted: %d bytes, type=%s (%s)",
            len(doc_data),
            ext,
            mime,
        )

        # Write to a temp file with unpredictable name (avoid TOCTOU race)
        try:
            tmp_fd = tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="airprint_")
            tmp_path = tmp_fd.name
            tmp_fd.write(doc_data)
            tmp_fd.close()
            logger.info("Temp file written: %s", tmp_path)
        except OSError:
            logger.exception("Failed to write temp file")
            self._send_ipp_error(req_id, IPP_STATUS_INTERNAL_ERROR)
            return

        try:
            copies = max(1, min(int(job_attrs_parsed.get("copies", "1") or 1), 99))
        except ValueError:
            copies = 1
        jid = JOBS.create(self.printer_name, job_attrs_parsed.get("job-name", ""),
                          job_attrs_parsed.get("requesting-user-name", ""))
        # Print in the background: the phone gets its answer at once and follows the job with
        # Get-Job-Attributes (pending -> processing -> completed / stopped / aborted / canceled).
        threading.Thread(
            target=_run_ipp_job, name=f"ipp-job-{jid}", daemon=True,
            args=(jid, self.printer_name, tmp_path, ext, media_size_mm, job_attrs_parsed, copies),
        ).start()

        job_attrs = struct.pack("!B", IPP_TAG_JOB)
        job_attrs += _encode_integer_attribute("job-id", jid)
        job_attrs += _encode_text_attribute(
            IPP_TAG_URI,
            "job-uri",
            f"ipp://{self.ctx.ip or self.host_ip}:{IPP_PORT}{self.url_prefix}/ipp/print/job/{jid}",
        )
        job_attrs += _encode_enum_attribute("job-state", job_tracking.JOB_PENDING)
        job_attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "job-state-reasons", "none")

        response = build_ipp_response(
            req_id, IPP_STATUS_OK, extra_groups=job_attrs,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Print-Job #%d accepted as job %d (%d copies) - printing in the background", req_id, jid, copies)

    def _handle_validate_job(
        self, req_id: int, ver_maj: int, ver_min: int,
    ) -> None:
        """Respond to Validate-Job with successful-ok."""
        response = build_ipp_response(
            req_id, IPP_STATUS_OK,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Validate-Job #%d → successful-ok", req_id)

    def _handle_get_printer_attributes(
        self, req_id: int, ver_maj: int, ver_min: int,
    ) -> None:
        """Return a rich set of printer attributes for discovery."""
        attrs = _build_printer_attributes(self.printer_name, self.ctx.ip or self.host_ip, self.ctx.cfg, self.url_prefix,
                                          mobile=is_mobile_client(self.headers.get("User-Agent", "")))
        response = build_ipp_response(
            req_id, IPP_STATUS_OK, extra_groups=attrs,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Get-Printer-Attributes #%d → sent attributes", req_id)

    def _job_group(self, job: dict) -> bytes:
        g = struct.pack("!B", IPP_TAG_JOB)
        g += _encode_integer_attribute("job-id", job["id"])
        g += _encode_text_attribute(
            IPP_TAG_URI, "job-uri",
            f"ipp://{self.ctx.ip or self.host_ip}:{IPP_PORT}{self.url_prefix}/ipp/print/job/{job['id']}")
        g += _encode_enum_attribute("job-state", job["state"])
        g += _encode_text_attribute(IPP_TAG_KEYWORD, "job-state-reasons", job["reasons"][0])
        for extra in job["reasons"][1:]:
            g += _encode_additional_value(IPP_TAG_KEYWORD, extra.encode("ascii"))
        if job.get("name"):
            g += _encode_text_attribute(IPP_TAG_NAME, "job-name", job["name"])
        return g

    @staticmethod
    def _requested_job_id(raw: bytes) -> int:
        a = extract_ipp_job_attributes(raw)
        if a.get("job-id", "").lstrip("-").isdigit():
            return int(a["job-id"])
        tail = (a.get("job-uri", "") or "").rstrip("/").rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else 0

    def _handle_get_jobs(
        self, req_id: int, ver_maj: int, ver_min: int, raw: bytes = b"",
    ) -> None:
        """List this printer's recent jobs (which-jobs=completed -> finished ones, otherwise the active ones)."""
        which = extract_ipp_job_attributes(raw).get("which-jobs", "not-completed") if raw else "not-completed"
        jobs = JOBS.list(self.printer_name, completed=(which == "completed"))
        groups = b"".join(self._job_group(j) for j in jobs)
        response = build_ipp_response(
            req_id, IPP_STATUS_OK, extra_groups=groups,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Get-Jobs #%d (%s) -> %d job(s)", req_id, which, len(jobs))

    def _handle_get_job_attributes(
        self, req_id: int, ver_maj: int, ver_min: int, raw: bytes = b"",
    ) -> None:
        """The real state of one job; an unknown id (e.g. from before a restart) is reported completed."""
        jid = self._requested_job_id(raw) if raw else req_id
        job = JOBS.get(jid) or {"id": jid, "state": job_tracking.JOB_COMPLETED,
                                "reasons": ["job-completed-successfully"], "name": ""}
        response = build_ipp_response(
            req_id, IPP_STATUS_OK, extra_groups=self._job_group(job),
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Get-Job-Attributes #%d -> job %d state %d", req_id, jid, job["state"])

    def _handle_cancel_job(
        self, req_id: int, ver_maj: int, ver_min: int, raw: bytes = b"",
    ) -> None:
        """Cancel a job: stops it and removes it from the Windows queue if it is already there."""
        jid = self._requested_job_id(raw) if raw else 0
        job = JOBS.get(jid)
        if job is None or job["state"] >= job_tracking.JOB_CANCELED:
            status = IPP_STATUS_OK if job is None else 0x0508          # not-possible for a finished job
        else:
            JOBS.cancel(jid)
            job_tracking.delete_spooler_job(self.printer_name, f"AirPrint job {jid}")
            status = IPP_STATUS_OK
        response = build_ipp_response(
            req_id, status,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Cancel-Job #%d -> job %d %s", req_id, jid, "canceled" if status == IPP_STATUS_OK else "not possible")

    # ------------------------------------------------------------------ #
    # Low-level response helpers                                          #
    # ------------------------------------------------------------------ #

    def _send_raw_ipp(self, data: bytes) -> None:
        """Send a raw IPP binary response over HTTP 200."""
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_ipp_error(self, req_id: int, status: int) -> None:
        """Send a minimal IPP error response."""
        response = build_ipp_response(req_id, status)
        self.send_response(200)  # IPP errors still ride on HTTP 200
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


# ---------------------------------------------------------------------------
# Threaded HTTP server wrapper
# ---------------------------------------------------------------------------

def _run_ipp_job(jid: int, printer_name: str, tmp_path: str, ext: str, media_size_mm, attrs: dict, copies: int) -> None:
    """Print one IPP job in the background and keep its state honest (runs in its own thread)."""
    job = JOBS.get(jid)
    spool_path = tmp_path
    doc_name = f"AirPrint job {jid}"
    import faulthandler
    _hang_log = open(_app_dir / "job-hang.log", "a")       # if a job thread ever stalls, its stack is written here after 90 s
    faulthandler.dump_traceback_later(90, repeat=False, file=_hang_log)
    try:
        if job and job["cancel"].is_set():
            return
        JOBS.set(jid, job_tracking.JOB_PROCESSING, ["job-printing"])
        if ext == ".urf":                      # Windows can't print Apple Raster natively
            spool_path = convert_urf_to_pdf(tmp_path)
            logger.info("Spool path after conversion: %s", spool_path)
        spool_to_printer(
            spool_path, printer_name, media_size_mm=media_size_mm,
            color_mode=attrs.get("print-color-mode", ""), copies=copies,
            quality=attrs.get("print-quality", ""), media_type=attrs.get("media-type", ""),
            doc_name=doc_name, opts=job_options(attrs),
        )
        result = job_tracking.wait_spooler_job(JOBS, jid, printer_name, doc_name)
        logger.info("Job %d finished: %s", jid, result)
    except Exception:  # noqa: BLE001
        logger.exception("Job %d failed", jid)
        JOBS.set(jid, job_tracking.JOB_ABORTED, ["aborted-by-system"])
    finally:
        faulthandler.cancel_dump_traceback_later()
        _hang_log.close()
        try:
            if (_app_dir / "job-hang.log").stat().st_size == 0:      # keep the file only if a stall was recorded
                (_app_dir / "job-hang.log").unlink()
        except OSError:
            pass
        for path in (tmp_path, spool_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def print_document_bytes(printer_name: str, doc_data: bytes, fmt: str = "") -> None:
    """Print a document received over WSD (same pipeline as an IPP Print-Job)."""
    ext, mime = detect_file_type(doc_data)
    logger.info("WSD document: %d bytes, declared=%r detected=%s (%s) head=%r", len(doc_data), fmt, ext, mime, doc_data[:24])
    fd = tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="airprint_wsd_")
    path = fd.name
    fd.write(doc_data)
    fd.close()
    spool = path
    try:
        if ext == ".urf":
            spool = convert_urf_to_pdf(path)
        elif ext == ".pwg":
            spool = wsd_device.pwg_raster_to_pdf(doc_data, path + ".pdf")
        media = None                       # PWG/PDF carry their own page size; only guess for size-less data
        spool_to_printer(spool, printer_name, media_size_mm=media)
    except Exception:  # noqa: BLE001
        logger.exception("WSD print failed")
    finally:
        for q in (path, spool):
            if q and os.path.exists(q):
                try:
                    os.remove(q)
                except OSError:
                    pass


class ThreadedIPPServer(HTTPServer):
    """HTTPServer that handles each request in a new daemon thread."""

    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address) -> None:  # type: ignore[override]
        """Start a daemon thread for each incoming connection."""
        t = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            daemon=True,
        )
        t.start()

    def process_request_thread(self, request, client_address) -> None:  # type: ignore[override]
        """Handle one request then close the socket."""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


# ---------------------------------------------------------------------------
# mDNS / DNS-SD registration
# ---------------------------------------------------------------------------

class MDNSAdvertiser:
    """
    Registers (and later unregisters) an ``_ipp._tcp.local.`` service
    using the ``zeroconf`` library so that AirPrint / IPP Everywhere
    clients discover the printer automatically.

    Also registers the ``_universal._sub._ipp._tcp.local.`` subtype
    which iOS requires to classify the service as AirPrint-compatible.
    """

    def __init__(
        self,
        printer_name: str,
        host_ip: str,
        port: int = IPP_PORT,
        scanner: bool = False,
        cfg: Optional[dict] = None,
        path_prefix: str = "",
    ) -> None:
        self._cfg = cfg
        self._prefix = path_prefix.strip("/") + "/" if path_prefix.strip("/") else ""
        self._scanner = scanner
        self._scan_info: Optional[ServiceInfo] = None
        self._zc: Optional[Zeroconf] = None
        self._zc_subtype: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None
        self._subtype_info: Optional[ServiceInfo] = None
        self._printer_name = printer_name
        self._host_ip = host_ip
        self._port = port

    def register(self) -> None:
        """Broadcast the service on the LAN."""
        hostname = socket.gethostname()
        display_name = get_display_name(self._printer_name, self._cfg)

        # Clean display name for mDNS instance label (allow spaces, escape dot/slashes, limit length)
        clean_instance = (
            display_name.replace("/", "_")
            .replace("\\", "_")
            .replace(".", "_")[:60]
        )
        service_name = f"{clean_instance}.{IPP_SERVICE_TYPE}"

        # Clean host name for target DNS host (must be a valid single-label DNS host)
        safe_host = (
            hostname.replace(" ", "-")
            .replace("/", "-")
            .replace("\\", "-")
            .replace(".", "-")[:60]
        )
        if self._cfg and self._cfg.get("ip"):
            safe_host = _slug(display_name)[:60]            # e.g. canon-printer-cabin-2.local -> the printer's own address

        # Unique UUID per printer + machine so multiple PCs don't collide on AirPrint clients
        printer_uuid_str = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{self._printer_name}@{hostname}"))

        txt_props = {
            "txtvers": "1",
            "qtotal": "1",
            "rp": f"{self._prefix}ipp/print",
            "ty": display_name,
            "product": f"({self._printer_name})",
            "pdl": "application/pdf,image/urf,image/jpeg,image/png,image/pwg-raster",
            "Color": "T" if printer_traits(self._printer_name, self._cfg)["color"] else "F",   # must match SRGB24 in URF
            "Duplex": "T" if printer_traits(self._printer_name, self._cfg)["duplex"] else "F",
            "adminurl": f"http://{self._host_ip}:{self._port}/",
            "priority": "50",
            "URF": printer_traits(self._printer_name, self._cfg)["urf"],
            "UUID": printer_uuid_str,
            "TLS": "none",
        }

        # --- Primary service: _ipp._tcp.local. ---
        self._info = ServiceInfo(
            type_=IPP_SERVICE_TYPE,
            name=service_name,
            addresses=[socket.inet_aton(self._host_ip)],
            port=self._port,
            properties=txt_props,
            server=f"{safe_host}.local.",
        )

        # Force Zeroconf to bind specifically to the Wi-Fi interface!
        # Windows often routes multicast traffic to the Ethernet/VPN adapter by default.
        self._zc = Zeroconf(interfaces=[self._host_ip])
        self._zc.register_service(self._info, strict=False)
        logger.info(
            "mDNS service registered: %s  (IP=%s  port=%d)",
            service_name,
            self._host_ip,
            self._port,
        )

        # --- AirPrint subtype: _universal._sub._ipp._tcp.local. ---
        airprint_subtype = f"_universal._sub.{IPP_SERVICE_TYPE}"

        self._subtype_info = ServiceInfo(
            type_=airprint_subtype,
            name=service_name,  # MUST use the primary name here!
            addresses=[socket.inet_aton(self._host_ip)],
            port=self._port,
            properties=txt_props,
            server=f"{safe_host}.local.",
        )
        try:
            self._zc_subtype = Zeroconf(interfaces=[self._host_ip])
            self._zc_subtype.register_service(self._subtype_info, strict=False)
            logger.info("mDNS subtype registered: %s", airprint_subtype)
        except Exception:
            logger.exception("Failed to register _universal subtype")

        # --- Scanner service: _uscan._tcp.local. (eSCL / AirScan) ---
        if self._scanner:
            scan_txt = {
                "txtvers": "1",
                "vers": "2.0",
                "ty": display_name,
                "rs": f"{self._prefix}eSCL",
                "pdl": "application/pdf,image/jpeg",
                "cs": "color,grayscale",
                "is": "platen",
                "duplex": "F",
                "adminurl": f"http://{self._host_ip}:{self._port}/",
                "UUID": printer_uuid_str,
            }
            self._scan_info = ServiceInfo(
                type_="_uscan._tcp.local.",
                name=f"{clean_instance}._uscan._tcp.local.",
                addresses=[socket.inet_aton(self._host_ip)],
                port=self._port,
                properties=scan_txt,
                server=f"{safe_host}.local.",
            )
            try:
                self._zc.register_service(self._scan_info, strict=False)
                logger.info("mDNS scanner service registered: %s", self._scan_info.name)
            except Exception:
                logger.exception("Failed to register _uscan service")


    def unregister(self) -> None:
        """Remove the service from the LAN."""
        if self._zc or self._zc_subtype:
            logger.info("Unregistering mDNS service …")
            
            if self._subtype_info is not None and self._zc_subtype is not None:
                try:
                    self._zc_subtype.unregister_service(self._subtype_info)
                except Exception:
                    pass
            
            if self._scan_info is not None and self._zc is not None:
                try:
                    self._zc.unregister_service(self._scan_info)
                except Exception:
                    pass

            if self._info is not None and self._zc is not None:
                try:
                    self._zc.unregister_service(self._info)
                except Exception:
                    pass

            try:
                if self._zc:
                    self._zc.close()
            except Exception:
                pass
            try:
                if self._zc_subtype:
                    self._zc_subtype.close()
            except Exception:
                pass

            self._zc = None
            self._zc_subtype = None
            self._info = None
            self._subtype_info = None
            logger.info("mDNS service unregistered.")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(shutdown_event: threading.Event) -> None:
    """Start the AirPrint bridge server."""
    logger.info("=" * 60)
    logger.info("AirPrint Bridge starting up")
    logger.info("=" * 60)

    global IPP_PORT
    IPP_PORT = int(_load_config().get("port") or IPP_PORT)        # "port" in config.json (default 631)

    # ---- Detect environment ----
    host_ip = get_local_ip()
    cfgs = get_printer_configs()
    if not cfgs:
        logger.critical("No usable printer (config.json or Windows default) — aborting.")
        sys.exit(1)
    IPPRequestHandler.host_ip = host_ip
    webui.attach(sys.modules[__name__])                                 # admin PIN + page
    for _c in cfgs:                                                      # first start: read the driver now so the announcements are right
        if driver_caps.peek(_c["printer"]) is None:
            try:
                driver_caps.load(_c["printer"])
            except Exception:  # noqa: BLE001
                logger.warning("could not read the driver of %r yet", _c["printer"])
    driver_caps.load_in_background([c["printer"] for c in cfgs])      # media types, borderless ... from each driver

    contexts: dict = {}
    mdns_list = []
    wsd_devs = []
    for n, cfg in enumerate(cfgs):
        printer_name = cfg["printer"]
        pip = str(cfg.get("ip") or host_ip)              # the printer's own address (config "ip") or the PC's
        if pip != host_ip:
            try:                                         # is that address really on this PC right now?
                _t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); _t.bind((pip, 0)); _t.close()
            except OSError:
                logger.error("configured ip %s is not assigned to this PC - announcing at %s instead", pip, host_ip)
                pip = host_ip
        ctx = PrinterCtx(cfg["id"], printer_name, cfg, pip)
        contexts[ctx.id] = ctx
        if pip != host_ip:
            logger.info("Printer %s is announced at its own address %s (PC address %s)", printer_name, pip, host_ip)
        first = (n == 0)
        prefix = "" if first else "/" + ctx.id           # the first printer keeps the old URLs in its announcements
        logger.info("Printer %d: %s  (id=%s)", n + 1, printer_name, ctx.id)

        wia_name = str(cfg.get("scanner") or "")
        if wia_name:
            if escl_scanner is None:
                logger.error("'scanner' set but escl_scanner.py is missing - scanning disabled")
            else:
                ctx.escl = escl_scanner.EsclScanner(
                    wia_name, get_display_name(printer_name, cfg),
                    str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{printer_name}@{socket.gethostname()}")),
                    adf=cfg.get("adf"), quality=int(cfg.get("scan_quality") or 85),
                    max_dpi=int(cfg.get("scan_max_dpi") or 1200))
                logger.info("Scanner sharing enabled for WIA device: %s", wia_name)

        mdns = MDNSAdvertiser(printer_name, pip, IPP_PORT, scanner=ctx.escl is not None, cfg=cfg, path_prefix=prefix)
        mdns.register()
        mdns_list.append(mdns)

        if cfg.get("wsd") and wsd_device is not None:
            display = get_display_name(printer_name, cfg)
            model = str(cfg.get("wsd_model") or display)
            maker = str(cfg.get("wsd_maker") or model.split()[0])
            wsd_port = int(cfg.get("wsd_port") or IPP_PORT)
            dev = wsd_device.WsdDevice(
                str(cfg.get("wsd_uuid") or uuid.uuid5(uuid.NAMESPACE_DNS, f"{printer_name}@{socket.gethostname()}")),
                pip, wsd_port, display, maker, model,
                scanner=ctx.escl,
                print_document=(lambda data, fmt, _p=printer_name: print_document_bytes(_p, data, fmt)),
                path_prefix=prefix,
                status_fn=(lambda _p=printer_name: job_tracking.printer_status(_p)),
                ppm=int(cfg.get("ppm") or 10),
                maker_url=str(cfg.get("wsd_url") or ""),
                firmware=str(cfg.get("wsd_firmware") or "1"),
                traits_fn=(lambda _p=printer_name, _c=cfg: printer_traits(_p, _c)))
            ctx.wsd = dev
            wsd_devs.append(dev)

    IPPRequestHandler.contexts = contexts
    IPPRequestHandler.default_ctx = contexts[cfgs[0]["id"]]

    wsd_disc = None
    if wsd_devs:
        try:
            wsd_disc = wsd_device.WsDiscovery(wsd_devs)
            wsd_disc.start()
        except Exception:  # noqa: BLE001
            logger.exception("WSD discovery could not start - WSD disabled")
            for c in contexts.values():
                c.wsd = None

    # ---- Start HTTP / IPP server ----
    server = ThreadedIPPServer(("0.0.0.0", IPP_PORT), IPPRequestHandler)
    logger.info("IPP server listening on 0.0.0.0:%d", IPP_PORT)

    def _cleanup() -> None:
        """atexit hook — ensures mDNS is always unregistered."""
        for m in mdns_list:
            m.unregister()
        if wsd_disc is not None:
            wsd_disc.stop()
        logger.info("AirPrint Bridge shut down cleanly.")

    atexit.register(_cleanup)

    for wp in sorted({d.port for d in wsd_devs if d.port != IPP_PORT}):
        try:
            wsd_server = ThreadedIPPServer(("0.0.0.0", wp), IPPRequestHandler)
            threading.Thread(target=wsd_server.serve_forever, daemon=True).start()
            logger.info("WSD HTTP endpoint also listening on 0.0.0.0:%d", wp)
        except OSError:
            logger.exception("could not listen on WSD port %d", wp)

    # Run the server in a background thread so we can wait on the event.
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info("Server thread started — ready to accept print jobs.")

    try:
        # Block the main thread until a shutdown signal fires.
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt caught — shutting down …")
    finally:
        server.shutdown()
        _cleanup()


class AirPrintBridgeService(win32serviceutil.ServiceFramework):
    _svc_name_ = "AirPrintBridge"
    _svc_display_name_ = "AirPrint Bridge Service"
    _svc_description_ = "Advertises local printers to Apple devices via mDNS and IPP."

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.shutdown_event = threading.Event()

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        self.shutdown_event.set()

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, '')
        )
        main(self.shutdown_event)


def _run_interactive() -> None:
    """Run the server interactively (not as a Windows service)."""
    shutdown_event = threading.Event()

    def _on_signal(signum=None, frame=None):
        shutdown_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)

    main(shutdown_event)


if __name__ == "__main__":
    import pywintypes
    if len(sys.argv) == 1:
        try:
            # Run natively as a Windows Service
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(AirPrintBridgeService)
            servicemanager.StartServiceCtrlDispatcher()
        except pywintypes.error as e:
            if e.winerror == 1063:
                print("Running interactively (not started by Service Control Manager)...")
                _run_interactive()
            else:
                raise
    else:
        # Command-line usage
        if sys.argv[1] == 'debug':
            _run_interactive()
        else:
            win32serviceutil.HandleCommandLine(AirPrintBridgeService)
