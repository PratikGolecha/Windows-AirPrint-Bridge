"""
eSCL (AirScan) scanner service for AirPrint Bridge.

Lets phones, Macs and other computers scan from a USB scanner attached to this
Windows PC.  The scanner is driven through Windows Image Acquisition (WIA) and
exposed over the eSCL protocol (Apple AirScan / Mopria Scan), served on the
same HTTP port as the IPP printer endpoint.

Enabled by ``"scanner": "<WIA device name>"`` in config.json.
Flat-bed (platen) scanning only: JPEG or PDF, colour or grey, 75-600 dpi.
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Dict, Optional, Tuple

logger = logging.getLogger("airprint_bridge")

NS_PWG = "http://www.pwg.org/schemas/2010/12/sm"
NS_SCAN = "http://schemas.hp.com/imaging/escl/2011/05/03"

WIA_FORMAT_JPEG = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"
WIA_TYPE_SCANNER = 1
WIA_INTENT_COLOR = 1
WIA_INTENT_GRAY = 2

RESOLUTIONS = (75, 100, 150, 200, 300, 600)
# Platen size in 1/300 inch (A4 height, Letter width)
MAX_W, MAX_H = 2550, 3508
MAX_W_A4 = 2480                   # full glass width the driver returns (A4, 1/300 inch)

SCAN_LOCK = threading.Lock()   # one physical scan at a time


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_scan_settings(xml_bytes: bytes) -> Dict[str, object]:
    """Pull the few eSCL ScanSettings we honour out of the request XML."""
    s: Dict[str, object] = {
        "dpi": 300, "color": True, "format": "image/jpeg",
        "region": None,          # (x, y, w, h) in 1/300 inch
    }
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        logger.warning("eSCL: unreadable ScanSettings, using defaults")
        return s
    region: Dict[str, int] = {}
    for el in root.iter():
        name, text = _local(el.tag), (el.text or "").strip()
        if not text:
            continue
        try:
            if name == "XResolution":
                s["dpi"] = min(RESOLUTIONS, key=lambda r: abs(r - int(text)))
            elif name == "ColorMode":
                s["color"] = text.upper().startswith("RGB")
            elif name in ("DocumentFormat", "DocumentFormatExt"):
                if text in ("application/pdf", "image/jpeg"):
                    s["format"] = text
            elif name in ("Width", "Height", "XOffset", "YOffset"):
                region[name] = int(text)
        except ValueError:
            continue
    if {"Width", "Height"} <= region.keys():
        s["region"] = (region.get("XOffset", 0), region.get("YOffset", 0),
                       region["Width"], region["Height"])
    return s


class EsclScanner:
    """eSCL front-end for one WIA scanner."""

    def __init__(self, wia_name: str, display_name: str, uuid_str: str) -> None:
        self.wia_name = wia_name
        self.display_name = display_name
        self.uuid = uuid_str
        self.jobs: Dict[str, dict] = {}
        self._jobs_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # XML documents                                                       #
    # ------------------------------------------------------------------ #
    def capabilities_xml(self) -> bytes:
        res = "".join(
            f"<scan:DiscreteResolution><scan:XResolution>{r}</scan:XResolution>"
            f"<scan:YResolution>{r}</scan:YResolution></scan:DiscreteResolution>"
            for r in RESOLUTIONS)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:pwg="{NS_PWG}" xmlns:scan="{NS_SCAN}">
 <pwg:Version>2.0</pwg:Version>
 <pwg:MakeAndModel>{self.display_name}</pwg:MakeAndModel>
 <scan:UUID>{self.uuid}</scan:UUID>
 <scan:Platen>
  <scan:PlatenInputCaps>
   <scan:MinWidth>16</scan:MinWidth>
   <scan:MaxWidth>{MAX_W}</scan:MaxWidth>
   <scan:MinHeight>16</scan:MinHeight>
   <scan:MaxHeight>{MAX_H}</scan:MaxHeight>
   <scan:MaxScanRegions>1</scan:MaxScanRegions>
   <scan:SettingProfiles>
    <scan:SettingProfile>
     <scan:ColorModes>
      <scan:ColorMode>RGB24</scan:ColorMode>
      <scan:ColorMode>Grayscale8</scan:ColorMode>
     </scan:ColorModes>
     <scan:DocumentFormats>
      <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
      <pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>
     </scan:DocumentFormats>
     <scan:SupportedResolutions>
      <scan:DiscreteResolutions>{res}</scan:DiscreteResolutions>
     </scan:SupportedResolutions>
    </scan:SettingProfile>
   </scan:SettingProfiles>
   <scan:SupportedIntents>
    <scan:Intent>Document</scan:Intent>
    <scan:Intent>TextAndGraphic</scan:Intent>
    <scan:Intent>Photo</scan:Intent>
   </scan:SupportedIntents>
  </scan:PlatenInputCaps>
 </scan:Platen>
</scan:ScannerCapabilities>"""
        return xml.encode("utf-8")

    def status_xml(self) -> bytes:
        busy = SCAN_LOCK.locked()
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:pwg="{NS_PWG}" xmlns:scan="{NS_SCAN}">
 <pwg:Version>2.0</pwg:Version>
 <pwg:State>{"Processing" if busy else "Idle"}</pwg:State>
</scan:ScannerStatus>"""
        return xml.encode("utf-8")

    # ------------------------------------------------------------------ #
    # Jobs                                                                #
    # ------------------------------------------------------------------ #
    def create_job(self, settings_xml: bytes) -> str:
        settings = parse_scan_settings(settings_xml)
        job_id = str(uuid.uuid4())
        job = {"settings": settings, "state": "pending", "data": None,
               "mime": settings["format"], "error": "", "delivered": False,
               "created": time.time()}
        with self._jobs_lock:
            self._prune()
            self.jobs[job_id] = job
        logger.info("eSCL job %s created: %s", job_id, settings)
        threading.Thread(target=self._run_job, args=(job_id,), daemon=True).start()
        return job_id

    def _prune(self) -> None:
        cutoff = time.time() - 900
        for jid in [j for j, v in self.jobs.items() if v["created"] < cutoff]:
            self.jobs.pop(jid, None)

    def _run_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        with SCAN_LOCK:
            job["state"] = "scanning"
            try:
                jpeg = self._wia_scan(job["settings"])
                if job["settings"]["format"] == "application/pdf":
                    from PIL import Image
                    buf = io.BytesIO()
                    Image.open(io.BytesIO(jpeg)).convert("RGB").save(
                        buf, "PDF", resolution=float(job["settings"]["dpi"]))
                    job["data"], job["mime"] = buf.getvalue(), "application/pdf"
                else:
                    job["data"], job["mime"] = jpeg, "image/jpeg"
                job["state"] = "done"
                logger.info("eSCL job %s done: %d bytes %s", job_id, len(job["data"]), job["mime"])
            except Exception as exc:  # noqa: BLE001
                job["state"], job["error"] = "error", str(exc)
                logger.exception("eSCL job %s failed", job_id)

    def next_document(self, job_id: str, timeout: float = 150.0) -> Optional[Tuple[bytes, str]]:
        """Block until the page is ready. None = no (more) documents."""
        job = self.jobs.get(job_id)
        if not job:
            return None
        end = time.time() + timeout
        while job["state"] in ("pending", "scanning") and time.time() < end:
            time.sleep(0.25)
        if job["state"] != "done" or job["delivered"]:
            return None
        job["delivered"] = True
        return job["data"], job["mime"]

    def job_error(self, job_id: str) -> str:
        job = self.jobs.get(job_id)
        return job["error"] if job else ""

    def delete_job(self, job_id: str) -> bool:
        with self._jobs_lock:
            return self.jobs.pop(job_id, None) is not None

    @staticmethod
    def _as_jpeg(data: bytes, color: bool, dpi: int, region=None) -> bytes:
        """WIA drivers often ignore the requested format and return a BMP/PNG, and
        some mishandle crop settings.  So: scan the whole glass, crop the requested
        region here (region is x, y, w, h in 1/300 inch) and always return a real JPEG."""
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        cropped = False
        if region:
            x, y, w, h = region
            k = img.width / float(MAX_W_A4)          # pixels per 1/300-inch unit
            left, top = max(0, int(x * k)), max(0, int(y * k))
            right, bottom = min(img.width, int((x + w) * k)), min(img.height, int((y + h) * k))
            if right - left > 8 and bottom - top > 8 and (
                    right - left < img.width * 0.97 or bottom - top < img.height * 0.97):
                img = img.crop((left, top, right, bottom))
                cropped = True
        # Some drivers ignore the requested resolution and snap to a supported one (Canon G4070: asked 100 dpi,
        # got 300 dpi pixels).  Resample so the pixel size really matches the dpi we label the file with.
        width_units = region[2] if (region and cropped) else MAX_W_A4          # 1/300 inch
        expected_w = max(1, round(width_units * dpi / 300.0))
        resized = False
        if abs(img.width - expected_w) > expected_w * 0.05:
            expected_h = max(1, round(img.height * expected_w / img.width))
            img = img.resize((expected_w, expected_h), Image.LANCZOS)
            resized = True
        if not cropped and not resized and data[:3] == b"\xff\xd8\xff" and (color or img.mode == "L"):
            return data                                   # already a proper JPEG, untouched
        img = img.convert("RGB" if color else "L")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, dpi=(dpi, dpi))
        return buf.getvalue()

    # ------------------------------------------------------------------ #
    # WIA                                                                 #
    # ------------------------------------------------------------------ #
    def _wia_scan(self, settings: Dict[str, object]) -> bytes:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            dm = win32com.client.Dispatch("WIA.DeviceManager")
            dev = None
            for info in dm.DeviceInfos:
                if info.Type == WIA_TYPE_SCANNER and info.Properties("Name").Value == self.wia_name:
                    dev = info.Connect()
                    break
            if dev is None:
                raise RuntimeError(f"WIA scanner '{self.wia_name}' not found")
            item = dev.Items.Item(1)

            dpi = int(settings["dpi"])

            def setp(name: str, value: int) -> None:
                try:
                    item.Properties(name).Value = value
                except Exception as exc:  # noqa: BLE001
                    logger.warning("WIA: could not set %r=%r (%s)", name, value, exc)

            setp("Horizontal Resolution", dpi)
            setp("Vertical Resolution", dpi)
            setp("Current Intent", WIA_INTENT_COLOR if settings["color"] else WIA_INTENT_GRAY)
            image = item.Transfer(WIA_FORMAT_JPEG)
            fd, path = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            os.remove(path)                       # WIA SaveFile refuses to overwrite
            try:
                image.SaveFile(path)
                with open(path, "rb") as fh:
                    data = fh.read()
                return self._as_jpeg(data, bool(settings["color"]), dpi, settings.get("region"))
            finally:
                if os.path.exists(path):
                    os.remove(path)
        finally:
            pythoncom.CoUninitialize()
