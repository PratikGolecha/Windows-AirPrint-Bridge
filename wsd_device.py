"""
WSD (Web Services for Devices) print + scan device for AirPrint Bridge.

Makes this PC's shared printer/scanner look to Windows exactly like a network multifunction printer
(Brother, HP ...): one "Add device" click installs the printer AND the scanner (visible to the Windows
Scan app / WIA).  Modelled byte-for-byte on what a real Brother DCP-L3551CDW answers - see
reference/wsd-brother-1/ in the notes folder.

Pieces
  * WS-Discovery responder (UDP 3702, multicast 239.255.255.250): Hello / Probe / Resolve / Bye
  * HTTP SOAP endpoints served by the bridge's own HTTP server under /WebServices/
        Device           WS-Transfer Get  -> device metadata (model, category, hosted services)
        ScannerService   GetScannerElements, ValidateScanTicket, CreateScanJob, RetrieveImage (MTOM), CancelJob, ...
        PrinterService   GetPrinterElements, CreatePrintJob, SendDocument (MTOM in), ... + eventing stubs

Enabled by  "wsd": true  in config.json.  Scanning needs "scanner" too.
"""
from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Callable, Dict, Optional, Tuple
from xml.sax.saxutils import escape

logger = logging.getLogger("airprint_bridge")

MCAST = "239.255.255.250"
WSD_PORT = 3702

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
NS_WSD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
NS_WSDP = "http://schemas.xmlsoap.org/ws/2006/02/devprof"
NS_WPRT = "http://schemas.microsoft.com/windows/2006/08/wdp/print"
NS_WSCN = "http://schemas.microsoft.com/windows/2006/08/wdp/scan"
NS_WSE = "http://schemas.xmlsoap.org/ws/2004/08/eventing"
NS_XOP = "http://www.w3.org/2004/08/xop/include"
ANON = NS_WSA + "/role/anonymous"
ACT_SCAN = NS_WSCN + "/"
ACT_PRINT = NS_WPRT + "/"

# WS-Scan works in 1/1000 inch.  Canon platen: A4 wide, ~A4 long.
SCAN_MAX_W, SCAN_MAX_H = 8268, 11693
SCAN_RESOLUTIONS = (100, 150, 200, 300, 600)


def _mid() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def envelope(action: str, relates_to: str, body: str, extra_ns: str = "", extra_header: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{NS_SOAP}" xmlns:wsa="{NS_WSA}" xmlns:wscn="{NS_WSCN}" '
        f'xmlns:wprt="{NS_WPRT}" xmlns:wse="{NS_WSE}" xmlns:xop="{NS_XOP}"{extra_ns}>'
        f'<SOAP-ENV:Header><wsa:MessageID>{_mid()}</wsa:MessageID>'
        + (f"<wsa:RelatesTo>{escape(relates_to)}</wsa:RelatesTo>" if relates_to else "")
        + f"<wsa:To>{ANON}</wsa:To><wsa:Action>{action}</wsa:Action>{extra_header}</SOAP-ENV:Header>"
        f"<SOAP-ENV:Body>{body}</SOAP-ENV:Body></SOAP-ENV:Envelope>"
    ).encode("utf-8")


def parse_request(data: bytes) -> Tuple[str, str, Optional[ET.Element]]:
    """-> (action, message-id, body-element)"""
    root = ET.fromstring(data)
    action = mid = ""
    body = None
    for el in root.iter():
        n = _local(el.tag)
        if n == "Action" and not action:
            action = (el.text or "").strip()
        elif n == "MessageID" and not mid:
            mid = (el.text or "").strip()
        elif n == "Body" and body is None:
            body = el
    return action, mid, body


def find_text(el: Optional[ET.Element], name: str, default: str = "") -> str:
    if el is None:
        return default
    for x in el.iter():
        if _local(x.tag) == name and (x.text or "").strip():
            return x.text.strip()
    return default


class WsdDevice:
    """One WSD MFP: identity + the SOAP handlers.  ``scanner`` is an EsclScanner (or None),
    ``print_document`` a callable(bytes, fmt) that prints a received document (or None)."""

    def __init__(self, uuid_str: str, host_ip: str, port: int, display_name: str, maker: str, model: str,
                 scanner=None, print_document: Optional[Callable[[bytes, str], None]] = None) -> None:
        self.uuid = uuid_str.lower()
        self.host_ip, self.port = host_ip, port
        self.display_name, self.maker, self.model = display_name, maker, model
        self.scanner = scanner
        self.print_document = print_document
        self.base = f"http://{host_ip}:{port}/WebServices"
        self.epr = f"urn:uuid:{self.uuid}"
        self.model_short = model[len(maker):].strip() if model.lower().startswith(maker.lower()) else model
        self.metadata_version = int(time.time()) & 0xFFFFFF
        self.scan_jobs: Dict[int, dict] = {}
        self.print_jobs: Dict[int, dict] = {}
        self._next_job = 1
        self.msgno = 0
        self._lock = threading.Lock()
        self.types = "wsdp:Device wprt:PrintDeviceType" + (" wscn:ScanDeviceType" if scanner else "")

    # ------------------------------------------------------------ dispatch --
    def handle(self, path: str, body: bytes, content_type: str = "") -> Tuple[int, str, bytes, Dict[str, str]]:
        svc = path.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        soap_bytes = body
        if "multipart" in (content_type or "").lower():          # MTOM: SOAP is the first (xop+xml) part
            soap_bytes = self._mtom_soap(body, content_type) or body
        try:
            action, mid, bel = parse_request(soap_bytes)
        except ET.ParseError:
            logger.warning("WSD: unreadable SOAP on %s", path)
            return 400, "text/plain", b"bad xml", {}
        name = action.rsplit("/", 1)[-1]
        logger.info("WSD %s %s", svc, name)
        if svc == "Device" or name in ("GetPrinterElements", "SetEventRate", "Subscribe"):
            logger.info("WSD raw request: %s", body[:1200].decode("utf-8", "replace"))
        try:
            if svc == "Device":
                if name == "Get":
                    return self._ok(self._device_metadata(mid))
                if name in ("Probe", "Resolve"):        # directed discovery over HTTP (Add-Printer -DeviceURL)
                    tag = "ProbeMatches" if name == "Probe" else "ResolveMatches"
                    one = tag[:-2]
                    b = (f'<wsd:{tag} xmlns:wsd="{NS_WSD}" xmlns:wsdp="{NS_WSDP}" xmlns:wprt="{NS_WPRT}" xmlns:wscn="{NS_WSCN}">'
                         f'<wsd:{one}><wsa:EndpointReference><wsa:Address>{self.epr}</wsa:Address></wsa:EndpointReference>'
                         f'<wsd:Types>{self.types}</wsd:Types><wsd:XAddrs>{self.base}/Device</wsd:XAddrs>'
                         f'<wsd:MetadataVersion>{self.metadata_version}</wsd:MetadataVersion></wsd:{one}></wsd:{tag}>')
                    self.msgno += 1
                    seq = f'<wsd:AppSequence xmlns:wsd="{NS_WSD}" InstanceId="{self.metadata_version}" MessageNumber="{self.msgno}"/>'
                    return self._ok(envelope(NS_WSD + "/" + tag, mid, b, extra_header=seq))
            elif svc == "ScannerService" and self.scanner is not None:
                r = self._scanner(name, mid, bel)
                if r:
                    return r
            elif svc == "PrinterService":
                r = self._printer(name, mid, bel, body, content_type)
                if r:
                    return r
            if name in ("Subscribe", "Renew", "Unsubscribe", "GetStatus"):
                return self._ok(self._eventing(name, mid, svc))
        except Exception:  # noqa: BLE001
            logger.exception("WSD %s/%s failed", svc, name)
            return self._fault(mid, "Receiver", "InternalError")
        logger.warning("WSD: unhandled %s/%s  action=%s\n%s", svc, name, action, body[:1500].decode("utf-8", "replace"))
        return self._fault(mid, "Sender", "ActionNotSupported")

    @staticmethod
    def _ok(b: bytes, ctype: str = "application/soap+xml; charset=utf-8", headers=None):
        return 200, ctype, b, headers or {}

    def _fault(self, mid: str, code: str, sub: str, ns: str = "wsa"):
        nsdecl = f' xmlns:wscn="{NS_WSCN}"' if ns == "wscn" else ""
        body = (f'<SOAP-ENV:Fault><SOAP-ENV:Code><SOAP-ENV:Value>SOAP-ENV:{code}</SOAP-ENV:Value>'
                f'<SOAP-ENV:Subcode><SOAP-ENV:Value{nsdecl}>{ns}:{sub}</SOAP-ENV:Value></SOAP-ENV:Subcode></SOAP-ENV:Code>'
                f'<SOAP-ENV:Reason><SOAP-ENV:Text xml:lang="en">{sub}</SOAP-ENV:Text></SOAP-ENV:Reason></SOAP-ENV:Fault>')
        return 500, "application/soap+xml; charset=utf-8", envelope(NS_WSA + "/fault", mid, body), {}

    # ------------------------------------------------------------ metadata --
    def _device_metadata(self, mid: str) -> bytes:
        n = escape
        hw = re.sub(r"[^A-Za-z0-9_-]+", "_", self.model)
        cat_a = "MFP Printers Scanners" if self.scanner else "Printers"
        cat_b = "PrintFax.MFP PrintFax.Printer Imaging.Scanner" if self.scanner else "PrintFax.Printer"
        hosted = self._hosted("PrinterService", "wprt", NS_WPRT, "PrinterServiceType", "PRINTER", hw)
        if self.scanner:
            hosted += self._hosted("ScannerService", "wscn", NS_WSCN, "ScannerServiceType", "SCANNER", hw)
        body = f"""<wsx:Metadata xmlns:wsx="http://schemas.xmlsoap.org/ws/2004/09/mex">
<wsx:MetadataSection Dialect="{NS_WSDP}/ThisModel"><wsdp:ThisModel xmlns:wsdp="{NS_WSDP}">
<wsdp:Manufacturer xml:lang="en-US">{n(self.maker)}</wsdp:Manufacturer>
<wsdp:ManufacturerUrl>http://www.canon.com</wsdp:ManufacturerUrl>
<wsdp:ModelName xml:lang="en-US">{n(self.model)}</wsdp:ModelName>
<wsdp:ModelNumber>{n(self.model)}</wsdp:ModelNumber>
<wsdp:PresentationUrl>http://{self.host_ip}:{self.port}/</wsdp:PresentationUrl>
<pnpx:DeviceCategory xmlns:pnpx="http://schemas.microsoft.com/windows/pnpx/2005/10">{cat_a}</pnpx:DeviceCategory>
<df:DeviceCategory xmlns:df="http://schemas.microsoft.com/windows/2008/09/devicefoundation">{cat_b}</df:DeviceCategory>
</wsdp:ThisModel></wsx:MetadataSection>
<wsx:MetadataSection Dialect="{NS_WSDP}/ThisDevice"><wsdp:ThisDevice xmlns:wsdp="{NS_WSDP}">
<wsdp:FriendlyName xml:lang="en-US">{n(self.display_name)}</wsdp:FriendlyName>
<wsdp:FirmwareVersion>1</wsdp:FirmwareVersion>
<wsdp:SerialNumber>{self.uuid[-12:].upper()}</wsdp:SerialNumber>
<df:ContainerId xmlns:df="http://schemas.microsoft.com/windows/2008/09/devicefoundation">{{{self.uuid}}}</df:ContainerId>
</wsdp:ThisDevice></wsx:MetadataSection>
<wsx:MetadataSection Dialect="{NS_WSDP}/Relationship"><wsdp:Relationship xmlns:wsdp="{NS_WSDP}" Type="{NS_WSDP}/host">
{hosted}</wsdp:Relationship></wsx:MetadataSection></wsx:Metadata>"""
        return envelope("http://schemas.xmlsoap.org/ws/2004/09/transfer/GetResponse", mid, body)

    def _hosted(self, svc: str, pfx: str, ns: str, stype: str, subsys: str, hw: str) -> str:
        return (f'<wsdp:Hosted><wsa:EndpointReference><wsa:Address>{self.base}/{svc}</wsa:Address></wsa:EndpointReference>'
                f'<wsdp:Types xmlns:{pfx}="{ns}">{pfx}:{stype}</wsdp:Types>'
                f'<wsdp:ServiceId>uri:{self.uuid}/{svc}</wsdp:ServiceId>'
                f'<pnpx:HardwareId xmlns:pnpx="http://schemas.microsoft.com/windows/pnpx/2005/10">'
                f'VEN_{self._ven()}&amp;DEV_{hw}&amp;SUBSYS_{subsys}</pnpx:HardwareId>'
                f'<pnpx:CompatibleId xmlns:pnpx="http://schemas.microsoft.com/windows/pnpx/2005/10">{ns}/{stype}</pnpx:CompatibleId>'
                f'</wsdp:Hosted>')

    def _ven(self) -> str:
        return "04a9" if self.maker.lower().startswith("canon") else "0000"

    # ------------------------------------------------------------ eventing --
    def _eventing(self, name: str, mid: str, svc: str) -> bytes:
        ident = f"urn:uuid:{uuid.uuid4()}"
        mgr = (f'<wse:SubscriptionManager><wsa:Address>{self.base}/{svc}</wsa:Address><wsa:ReferenceParameters>'
               f'<wse:Identifier>{ident}</wse:Identifier></wsa:ReferenceParameters></wse:SubscriptionManager>')
        if name == "Subscribe":
            return envelope(NS_WSE + "/SubscribeResponse", mid,
                            f"<wse:SubscribeResponse>{mgr}<wse:Expires>PT1H</wse:Expires></wse:SubscribeResponse>")
        if name == "Renew":
            return envelope(NS_WSE + "/RenewResponse", mid, "<wse:RenewResponse><wse:Expires>PT1H</wse:Expires></wse:RenewResponse>")
        if name == "GetStatus":
            return envelope(NS_WSE + "/GetStatusResponse", mid, "<wse:GetStatusResponse><wse:Expires>PT1H</wse:Expires></wse:GetStatusResponse>")
        return envelope(NS_WSE + "/UnsubscribeResponse", mid, "")

    # ------------------------------------------------------------- scanner --
    def _scanner(self, name: str, mid: str, bel):
        sname = escape(self.display_name)
        if name == "GetScannerElements":
            return self._ok(envelope(ACT_SCAN + "GetScannerElementsResponse", mid, self._scanner_elements(sname)))
        if name == "ValidateScanTicket":
            t = self._ticket(bel)
            return self._ok(envelope(ACT_SCAN + "ValidateScanTicketResponse", mid,
                                     "<wscn:ValidateScanTicketResponse><wscn:ValidationInfo><wscn:ValidTicket>1</wscn:ValidTicket>"
                                     f"{self._image_info(t)}</wscn:ValidationInfo></wscn:ValidateScanTicketResponse>"))
        if name == "CreateScanJob":
            t = self._ticket(bel)
            logger.info("WSD CreateScanJob request: %s", ET.tostring(bel, encoding="unicode")[:1500] if bel is not None else "")
            with self._lock:
                jid = self._next_job
                self._next_job += 1
            token = f"urn:uuid:{uuid.uuid4()}"
            settings = {"dpi": t["dpi"], "color": t["color"], "format": "image/jpeg", "region": t["region300"], "source": t["source"], "max_pages": t["images"]}
            esc_id = self.scanner.create_job_settings(settings)
            self.scan_jobs[jid] = {"token": token, "esc": esc_id, "ticket": t, "created": time.time()}
            logger.info("WSD scan job %d -> %s", jid, t)
            return self._ok(envelope(ACT_SCAN + "CreateScanJobResponse", mid,
                                     f"<wscn:CreateScanJobResponse><wscn:JobId>{jid}</wscn:JobId><wscn:JobToken>{token}</wscn:JobToken>"
                                     f"{self._image_info(t)}{self._final_params(t)}</wscn:CreateScanJobResponse>"))
        if name == "RetrieveImage":
            jid = int(find_text(bel, "JobId", "0") or 0)
            job = self.scan_jobs.get(jid)
            doc = self.scanner.next_document(job["esc"]) if job else None
            if doc is None:
                err = self.scanner.job_error(job["esc"]) if job else "unknown job"
                logger.info("WSD RetrieveImage job %s: no (more) images (%s)", jid, err)
                if err:
                    return self._fault(mid, "Receiver", "ServerErrorScanFailure", "wscn")
                return self._fault(mid, "Sender", "ClientErrorNoImagesAvailable", "wscn")
            return self._mtom_image(mid, doc[0])
        if name in ("CancelJob",):
            jid = int(find_text(bel, "JobId", "0") or 0)
            job = self.scan_jobs.pop(jid, None)
            if job:
                self.scanner.delete_job(job["esc"])
            return self._ok(envelope(ACT_SCAN + "CancelJobResponse", mid,
                                     f"<wscn:CancelJobResponse><wscn:JobId>{jid}</wscn:JobId></wscn:CancelJobResponse>"))
        if name == "GetActiveJobs":
            return self._ok(envelope(ACT_SCAN + "GetActiveJobsResponse", mid,
                                     "<wscn:GetActiveJobsResponse><wscn:ActiveJobs/></wscn:GetActiveJobsResponse>"))
        return None

    def _ticket(self, bel) -> dict:
        color = find_text(bel, "ColorProcessing", "RGB24")
        dpi = self._res(bel)
        x = int(find_text(bel, "ScanRegionXOffset", "0"))
        y = int(find_text(bel, "ScanRegionYOffset", "0"))
        w = int(find_text(bel, "ScanRegionWidth", str(SCAN_MAX_W)))
        h = int(find_text(bel, "ScanRegionHeight", str(SCAN_MAX_H)))
        w, h = min(w, SCAN_MAX_W), min(h, SCAN_MAX_H)
        dpi = min(SCAN_RESOLUTIONS, key=lambda r: abs(r - dpi))
        k = 300.0 / 1000.0                     # 1/1000 inch -> 1/300 inch
        full = (x == 0 and y == 0 and w >= SCAN_MAX_W - 10 and h >= SCAN_MAX_H - 10)
        src = find_text(bel, "InputSource", "")
        auto = not src or src.lower() == "auto"        # Scan app "Auto": the ticket names no source - the scanner decides
        src = src or "Auto"
        try:
            images = int(find_text(bel, "ImagesToTransfer", "0" if auto else "1"))
        except ValueError:
            images = 1
        return {"source": "auto" if auto else ("feeder" if src.upper().startswith("ADF") else "platen"), "srcname": src, "auto": auto, "images": images,
                "color": color.upper().startswith("RGB"), "colorname": color, "dpi": dpi,
                "x": x, "y": y, "w": w, "h": h,
                "region300": None if full else (round(x * k), round(y * k), max(1, round(w * k)), max(1, round(h * k)))}

    @staticmethod
    def _res(bel) -> int:
        if bel is None:
            return 300
        for el in bel.iter():
            if _local(el.tag) == "Resolution":
                for c in el:
                    if _local(c.tag) == "Width" and (c.text or "").strip().isdigit():
                        return int(c.text.strip())
        return 300

    def _image_info(self, t: dict) -> str:
        px = max(1, round(t["w"] * t["dpi"] / 1000.0))
        ln = max(1, round(t["h"] * t["dpi"] / 1000.0))
        bpl = px * (3 if t["color"] else 1)
        return (f"<wscn:ImageInformation><wscn:MediaFrontImageInfo><wscn:PixelsPerLine>{px}</wscn:PixelsPerLine>"
                f"<wscn:NumberOfLines>{ln}</wscn:NumberOfLines><wscn:BytesPerLine>{bpl}</wscn:BytesPerLine>"
                "</wscn:MediaFrontImageInfo></wscn:ImageInformation>")

    def _final_params(self, t: dict) -> str:
        return (f'<wscn:DocumentFinalParameters><wscn:Format>exif</wscn:Format>'
                f'<wscn:CompressionQualityFactor wscn:UsedDefault="true">100</wscn:CompressionQualityFactor>'
                f'<wscn:ImagesToTransfer>{t["images"] if t["source"] == "feeder" else 1}</wscn:ImagesToTransfer><wscn:InputSource>{"Platen" if t["auto"] else escape(t["srcname"])}</wscn:InputSource>'
                f'<wscn:ContentType wscn:UsedDefault="true">Auto</wscn:ContentType>'
                f'<wscn:InputSize><wscn:InputMediaSize><wscn:Width>{SCAN_MAX_W}</wscn:Width><wscn:Height>{SCAN_MAX_H}</wscn:Height>'
                f'</wscn:InputMediaSize></wscn:InputSize>'
                f'<wscn:Exposure><wscn:ExposureSettings><wscn:Contrast wscn:UsedDefault="true">0</wscn:Contrast>'
                f'<wscn:Brightness wscn:UsedDefault="true">0</wscn:Brightness><wscn:Sharpness wscn:UsedDefault="true">0</wscn:Sharpness>'
                f'</wscn:ExposureSettings></wscn:Exposure>'
                f'<wscn:Scaling><wscn:ScalingWidth wscn:UsedDefault="true">100</wscn:ScalingWidth>'
                f'<wscn:ScalingHeight wscn:UsedDefault="true">100</wscn:ScalingHeight></wscn:Scaling>'
                f'<wscn:Rotation wscn:UsedDefault="true">0</wscn:Rotation>'
                f'<wscn:MediaSides><wscn:MediaFront><wscn:ScanRegion><wscn:ScanRegionXOffset>{t["x"]}</wscn:ScanRegionXOffset>'
                f'<wscn:ScanRegionYOffset>{t["y"]}</wscn:ScanRegionYOffset><wscn:ScanRegionWidth>{t["w"]}</wscn:ScanRegionWidth>'
                f'<wscn:ScanRegionHeight>{t["h"]}</wscn:ScanRegionHeight></wscn:ScanRegion>'
                f'<wscn:ColorProcessing>{escape(t["colorname"])}</wscn:ColorProcessing>'
                f'<wscn:Resolution><wscn:Width>{t["dpi"]}</wscn:Width><wscn:Height>{t["dpi"]}</wscn:Height></wscn:Resolution>'
                f'</wscn:MediaFront></wscn:MediaSides></wscn:DocumentFinalParameters>')

    def _mtom_image(self, mid: str, jpeg: bytes):
        boundary = str(uuid.uuid4())
        start = f"{uuid.uuid4()}@uuid"
        img = f"{uuid.uuid4()}@uuid"
        soap = envelope(ACT_SCAN + "RetrieveImageResponse", mid,
                        f'<wscn:RetrieveImageResponse><wscn:ScanData><xop:Include href="cid:{img}"/></wscn:ScanData></wscn:RetrieveImageResponse>')
        payload = (f"--{boundary}\r\nContent-Type: application/xop+xml; type=\"application/soap+xml\"; charset=utf-8\r\n"
                   f"Content-Transfer-Encoding: binary\r\nContent-ID: <{start}>\r\n\r\n").encode() + soap + \
                  (f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Transfer-Encoding: binary\r\n"
                   f"Content-ID: <{img}>\r\n\r\n").encode() + jpeg + f"\r\n--{boundary}--\r\n".encode()
        ctype = (f'Multipart/Related; boundary="{boundary}"; type="application/xop+xml"; '
                 f'start="<{start}>"; start-info="application/soap+xml"')
        return 200, ctype, payload, {}

    def _scanner_elements(self, sname: str) -> str:
        res_w = "".join(f"<wscn:Width>{r}</wscn:Width>" for r in SCAN_RESOLUTIONS)
        res_h = "".join(f"<wscn:Height>{r}</wscn:Height>" for r in SCAN_RESOLUTIONS)
        color = "<wscn:ColorEntry>Grayscale8</wscn:ColorEntry><wscn:ColorEntry>RGB24</wscn:ColorEntry>"
        return f"""<wscn:GetScannerElementsResponse><wscn:ScannerElements>
<wscn:ElementData Name="wscn:ScannerDescription" Valid="true"><wscn:ScannerDescription><wscn:ScannerName xml:lang="en-US">{sname}</wscn:ScannerName></wscn:ScannerDescription></wscn:ElementData>
<wscn:ElementData Name="wscn:ScannerConfiguration" Valid="true"><wscn:ScannerConfiguration>
<wscn:DeviceSettings>
<wscn:FormatsSupported><wscn:FormatValue>exif</wscn:FormatValue></wscn:FormatsSupported>
<wscn:CompressionQualityFactorSupported><wscn:MinValue>100</wscn:MinValue><wscn:MaxValue>100</wscn:MaxValue></wscn:CompressionQualityFactorSupported>
<wscn:ContentTypesSupported><wscn:ContentTypeValue>Auto</wscn:ContentTypeValue></wscn:ContentTypesSupported>
<wscn:DocumentSizeAutoDetectSupported>0</wscn:DocumentSizeAutoDetectSupported>
<wscn:AutoExposureSupported>0</wscn:AutoExposureSupported>
<wscn:BrightnessSupported>0</wscn:BrightnessSupported>
<wscn:ContrastSupported>0</wscn:ContrastSupported>
<wscn:ScalingRangeSupported><wscn:ScalingWidth><wscn:MinValue>100</wscn:MinValue><wscn:MaxValue>100</wscn:MaxValue></wscn:ScalingWidth><wscn:ScalingHeight><wscn:MinValue>100</wscn:MinValue><wscn:MaxValue>100</wscn:MaxValue></wscn:ScalingHeight></wscn:ScalingRangeSupported>
<wscn:RotationsSupported><wscn:RotationValue>0</wscn:RotationValue></wscn:RotationsSupported>
</wscn:DeviceSettings>
<wscn:Platen>
<wscn:PlatenOpticalResolution><wscn:Width>600</wscn:Width><wscn:Height>1200</wscn:Height></wscn:PlatenOpticalResolution>
<wscn:PlatenResolutions><wscn:Widths>{res_w}</wscn:Widths><wscn:Heights>{res_h}</wscn:Heights></wscn:PlatenResolutions>
<wscn:PlatenColor>{color}</wscn:PlatenColor>
<wscn:PlatenMinimumSize><wscn:Width>556</wscn:Width><wscn:Height>556</wscn:Height></wscn:PlatenMinimumSize>
<wscn:PlatenMaximumSize><wscn:Width>{SCAN_MAX_W}</wscn:Width><wscn:Height>{SCAN_MAX_H}</wscn:Height></wscn:PlatenMaximumSize>
</wscn:Platen>
<wscn:ADF><wscn:ADFSupportsDuplex>0</wscn:ADFSupportsDuplex><wscn:ADFFront>
<wscn:ADFOpticalResolution><wscn:Width>600</wscn:Width><wscn:Height>600</wscn:Height></wscn:ADFOpticalResolution>
<wscn:ADFResolutions><wscn:Widths>{res_w}</wscn:Widths><wscn:Heights>{res_h}</wscn:Heights></wscn:ADFResolutions>
<wscn:ADFColor>{color}</wscn:ADFColor>
<wscn:ADFMinimumSize><wscn:Width>556</wscn:Width><wscn:Height>556</wscn:Height></wscn:ADFMinimumSize>
<wscn:ADFMaximumSize><wscn:Width>{SCAN_MAX_W}</wscn:Width><wscn:Height>{SCAN_MAX_H}</wscn:Height></wscn:ADFMaximumSize>
</wscn:ADFFront></wscn:ADF>
</wscn:ScannerConfiguration></wscn:ElementData>
<wscn:ElementData Name="wscn:ScannerStatus" Valid="true"><wscn:ScannerStatus><wscn:ScannerCurrentTime>{time.strftime('%Y-%m-%dT%H:%M:%S')}</wscn:ScannerCurrentTime><wscn:ScannerState>Idle</wscn:ScannerState><wscn:ActiveConditions/></wscn:ScannerStatus></wscn:ElementData>
<wscn:ElementData Name="wscn:DefaultScanTicket" Valid="true"><wscn:DefaultScanTicket>
<wscn:JobDescription><wscn:JobName>JobName</wscn:JobName><wscn:JobOriginatingUserName>JobOriginatingUserName</wscn:JobOriginatingUserName><wscn:JobInformation>JobInformation</wscn:JobInformation></wscn:JobDescription>
<wscn:DocumentParameters><wscn:Format>exif</wscn:Format><wscn:CompressionQualityFactor>100</wscn:CompressionQualityFactor><wscn:ImagesToTransfer>1</wscn:ImagesToTransfer><wscn:InputSource>Platen</wscn:InputSource><wscn:ContentType>Auto</wscn:ContentType>
<wscn:InputSize><wscn:InputMediaSize><wscn:Width>{SCAN_MAX_W}</wscn:Width><wscn:Height>{SCAN_MAX_H}</wscn:Height></wscn:InputMediaSize></wscn:InputSize>
<wscn:Exposure><wscn:ExposureSettings><wscn:Contrast>0</wscn:Contrast><wscn:Brightness>0</wscn:Brightness><wscn:Sharpness>0</wscn:Sharpness></wscn:ExposureSettings></wscn:Exposure>
<wscn:Scaling><wscn:ScalingWidth>100</wscn:ScalingWidth><wscn:ScalingHeight>100</wscn:ScalingHeight></wscn:Scaling>
<wscn:Rotation>0</wscn:Rotation>
<wscn:MediaSides><wscn:MediaFront><wscn:ScanRegion><wscn:ScanRegionXOffset>0</wscn:ScanRegionXOffset><wscn:ScanRegionYOffset>0</wscn:ScanRegionYOffset><wscn:ScanRegionWidth>{SCAN_MAX_W}</wscn:ScanRegionWidth><wscn:ScanRegionHeight>{SCAN_MAX_H}</wscn:ScanRegionHeight></wscn:ScanRegion>
<wscn:ColorProcessing>RGB24</wscn:ColorProcessing><wscn:Resolution><wscn:Width>300</wscn:Width><wscn:Height>300</wscn:Height></wscn:Resolution></wscn:MediaFront></wscn:MediaSides>
</wscn:DocumentParameters></wscn:DefaultScanTicket></wscn:ElementData>
</wscn:ScannerElements></wscn:GetScannerElementsResponse>"""

    # ------------------------------------------------------------- printer --
    def _printer(self, name: str, mid: str, bel, raw: bytes, ctype: str):
        pname = escape(self.display_name)
        if name == "GetPrinterElements":
            devid = escape(f"MFG:{self.maker};CMD:URF,PWGRaster;MDL:{self.model_short};CLS:PRINTER;CID:MS_PWGR;URF:SRGB24,W8,CP1,IS1,RS300,V1.4,DM1;")
            body = f"""<wprt:GetPrinterElementsResponse><wprt:PrinterElements>
<wprt:ElementData Name="wprt:PrinterDescription" Valid="true"><wprt:PrinterDescription>
<wprt:ColorSupported>1</wprt:ColorSupported><wprt:DeviceId>{devid}</wprt:DeviceId>
<wprt:MultipleDocumentJobsSupported>false</wprt:MultipleDocumentJobsSupported>
<wprt:PagesPerMinute>8</wprt:PagesPerMinute><wprt:PagesPerMinuteColor>4</wprt:PagesPerMinuteColor>
<wprt:PrinterName xml:lang="en-US">{pname}</wprt:PrinterName></wprt:PrinterDescription></wprt:ElementData>
<wprt:ElementData Name="wprt:PrinterConfiguration" Valid="true"><wprt:PrinterConfiguration><wprt:PrinterEventRate>1</wprt:PrinterEventRate>
<wprt:Finishings><wprt:CollationSupported>0</wprt:CollationSupported><wprt:JogOffsetSupported>0</wprt:JogOffsetSupported>
<wprt:DuplexerInstalled>0</wprt:DuplexerInstalled><wprt:StaplerInstalled>0</wprt:StaplerInstalled><wprt:HolePunchInstalled>0</wprt:HolePunchInstalled></wprt:Finishings>
</wprt:PrinterConfiguration></wprt:ElementData>
<wprt:ElementData Name="wprt:PrinterStatus" Valid="true"><wprt:PrinterStatus><wprt:PrinterCurrentTime>{time.strftime('%Y-%m-%dT%H:%M:%S')}</wprt:PrinterCurrentTime>
<wprt:PrinterState>Idle</wprt:PrinterState><wprt:PrinterPrimaryStateReason>None</wprt:PrinterPrimaryStateReason><wprt:QueuedJobCount>0</wprt:QueuedJobCount></wprt:PrinterStatus></wprt:ElementData>
<wprt:ElementData Name="wprt:DefaultPrintTicket" Valid="true"><wprt:DefaultPrintTicket><wprt:JobDescription><wprt:JobName>DefaultName</wprt:JobName>
<wprt:JobOriginatingUserName>DefaultUser</wprt:JobOriginatingUserName></wprt:JobDescription></wprt:DefaultPrintTicket></wprt:ElementData>
</wprt:PrinterElements></wprt:GetPrinterElementsResponse>"""
            return self._ok(envelope(ACT_PRINT + "GetPrinterElementsResponse", mid, body))
        if name == "CreatePrintJob":
            with self._lock:
                jid = self._next_job
                self._next_job += 1
            self.print_jobs[jid] = {"created": time.time()}
            return self._ok(envelope(ACT_PRINT + "CreatePrintJobResponse", mid,
                                     f"<wprt:CreatePrintJobResponse><wprt:JobId>{jid}</wprt:JobId></wprt:CreatePrintJobResponse>"))
        if name == "SendDocument":
            jid = int(find_text(bel, "JobId", "0") or 0)
            fmt = find_text(bel, "DocumentFormat", "")
            data = self._mtom_payload(raw, ctype)
            logger.info("WSD SendDocument job %d format=%r %d bytes", jid, fmt, len(data))
            if self.print_document is not None and data:
                threading.Thread(target=self.print_document, args=(data, fmt), daemon=True).start()
            return self._ok(envelope(ACT_PRINT + "SendDocumentResponse", mid,
                                     f"<wprt:SendDocumentResponse><wprt:JobId>{jid}</wprt:JobId></wprt:SendDocumentResponse>"))
        if name == "SetEventRate":
            return self._ok(envelope(ACT_PRINT + "SetEventRateResponse", mid,
                                     "<wprt:SetEventRateResponse><wprt:PrinterEventRate>1</wprt:PrinterEventRate></wprt:SetEventRateResponse>"))
        if name == "GetActiveJobs":
            return self._ok(envelope(ACT_PRINT + "GetActiveJobsResponse", mid,
                                     "<wprt:GetActiveJobsResponse><wprt:ActiveJobs/></wprt:GetActiveJobsResponse>"))
        if name == "CancelJob":
            jid = int(find_text(bel, "JobId", "0") or 0)
            return self._ok(envelope(ACT_PRINT + "CancelJobResponse", mid,
                                     f"<wprt:CancelJobResponse><wprt:JobId>{jid}</wprt:JobId></wprt:CancelJobResponse>"))
        if name == "GetJobElements":
            jid = int(find_text(bel, "JobId", "0") or 0)
            return self._ok(envelope(ACT_PRINT + "GetJobElementsResponse", mid,
                                     f"<wprt:GetJobElementsResponse><wprt:JobElements><wprt:ElementData Name=\"wprt:JobStatus\" Valid=\"true\">"
                                     f"<wprt:JobStatus><wprt:JobId>{jid}</wprt:JobId><wprt:JobState>Completed</wprt:JobState></wprt:JobStatus>"
                                     f"</wprt:ElementData></wprt:JobElements></wprt:GetJobElementsResponse>"))
        return None

    @staticmethod
    def _mtom_soap(raw: bytes, ctype: str) -> bytes:
        m = re.search(r'boundary="?([^";]+)"?', ctype or "", re.I)
        if not m:
            return b""
        for p in raw.split(b"--" + m.group(1).encode())[1:]:
            head, _, data = p.partition(b"\r\n\r\n")
            if (b"soap+xml" in head.lower() or b"xop+xml" in head.lower()) and not p.startswith(b"--"):
                return data.rstrip(b"\r\n")
        return b""

    @staticmethod
    def _mtom_payload(raw: bytes, ctype: str) -> bytes:
        """Return the binary attachment of an MTOM/multipart SendDocument request (or the raw body if not multipart)."""
        m = re.search(r'boundary="?([^";]+)"?', ctype or "", re.I)
        if not m:
            return b""
        sep = b"--" + m.group(1).encode()
        parts = raw.split(sep)
        best = b""
        for p in parts[1:]:
            if p.startswith(b"--"):
                continue
            head, _, data = p.partition(b"\r\n\r\n")
            if b"xop+xml" in head.lower() or b"soap+xml" in head.lower():
                continue
            data = data[:-2] if data.endswith(b"\r\n") else data
            if len(data) > len(best):
                best = data
        return best


# ============================================================================ discovery ==
class WsDiscovery:
    """WS-Discovery target service: multicast Hello on start, answer Probe/Resolve, Bye on stop."""

    def __init__(self, dev: WsdDevice) -> None:
        self.dev = dev
        self.sock: Optional[socket.socket] = None
        self.instance = int(time.time())
        self.msgno = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", WSD_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(MCAST), socket.inet_aton(self.dev.host_ip))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.dev.host_ip))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        s.settimeout(1.0)
        self.sock = s
        threading.Thread(target=self._loop, daemon=True, name="wsd-discovery").start()
        self._hello()
        logger.info("WSD discovery listening on UDP %d as %s -> %s/Device", WSD_PORT, self.dev.epr, self.dev.base)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._send_multicast(NS_WSD + "/Bye", "<wsd:Bye><wsa:EndpointReference><wsa:Address>"
                                 f"{self.dev.epr}</wsa:Address></wsa:EndpointReference></wsd:Bye>", "urn:schemas-xmlsoap-org:ws:2005:04:discovery")
        except Exception:  # noqa: BLE001
            pass

    def _env(self, action: str, to: str, relates: str, body: str) -> bytes:
        with self._lock:
            self.msgno += 1
            n = self.msgno
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{NS_SOAP}" xmlns:wsa="{NS_WSA}" xmlns:wsd="{NS_WSD}" '
            f'xmlns:wsdp="{NS_WSDP}" xmlns:wprt="{NS_WPRT}" xmlns:wscn="{NS_WSCN}"><SOAP-ENV:Header>'
            f"<wsa:To>{to}</wsa:To><wsa:Action>{action}</wsa:Action><wsa:MessageID>{_mid()}</wsa:MessageID>"
            + (f"<wsa:RelatesTo>{escape(relates)}</wsa:RelatesTo>" if relates else "")
            + f'<wsd:AppSequence InstanceId="{self.instance}" MessageNumber="{n}"/></SOAP-ENV:Header>'
            f"<SOAP-ENV:Body>{body}</SOAP-ENV:Body></SOAP-ENV:Envelope>"
        ).encode("utf-8")

    def _match_body(self, tag: str) -> str:
        d = self.dev
        return (f"<wsa:EndpointReference><wsa:Address>{d.epr}</wsa:Address></wsa:EndpointReference>"
                f"<wsd:Types>{d.types}</wsd:Types><wsd:XAddrs>{d.base}/Device</wsd:XAddrs>"
                f"<wsd:MetadataVersion>{d.metadata_version}</wsd:MetadataVersion>")

    def _send_multicast(self, action: str, body: str, to: str) -> None:
        assert self.sock
        msg = self._env(action, to, "", body)
        for _ in range(2):
            self.sock.sendto(msg, (MCAST, WSD_PORT))
            time.sleep(0.2)

    def _hello(self) -> None:
        self._send_multicast(NS_WSD + "/Hello", f"<wsd:Hello>{self._match_body('Hello')}</wsd:Hello>",
                             "urn:schemas-xmlsoap-org:ws:2005:04:discovery")

    def _loop(self) -> None:
        assert self.sock
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                action, mid, body = parse_request(data)
            except ET.ParseError:
                continue
            name = action.rsplit("/", 1)[-1]
            try:
                if name == "Probe":
                    want = find_text(body, "Types", "")
                    if not want or any(t.split(":")[-1] in self.dev.types for t in want.split()):
                        logger.info("WSD Probe from %s (types=%r) -> ProbeMatch", addr[0], want)
                        self.sock.sendto(self._env(NS_WSD + "/ProbeMatches", ANON, mid,
                                                   f"<wsd:ProbeMatches><wsd:ProbeMatch>{self._match_body('')}</wsd:ProbeMatch></wsd:ProbeMatches>"),
                                         addr)
                elif name == "Resolve":
                    if self.dev.uuid in data.decode("utf-8", "replace").lower():
                        logger.info("WSD Resolve from %s -> ResolveMatch", addr[0])
                        self.sock.sendto(self._env(NS_WSD + "/ResolveMatches", ANON, mid,
                                                   f"<wsd:ResolveMatches><wsd:ResolveMatch>{self._match_body('')}</wsd:ResolveMatch></wsd:ResolveMatches>"),
                                         addr)
            except Exception:  # noqa: BLE001
                logger.exception("WSD discovery reply failed")


# ============================================================================ PWG raster ==
def pwg_raster_to_pdf(data: bytes, out_path: str) -> str:
    """Decode a PWG Raster stream ('RaS2' + 1796-byte page headers + PackBits-style rows) to a PDF file.
    Page size comes from the page header (points), so paper size follows what the Windows driver chose."""
    import io
    import fitz                      # PyMuPDF (already needed for printing)
    from PIL import Image

    if data[:4] != b"RaS2":
        raise ValueError("not PWG raster")
    pos, doc = 4, fitz.open()
    while pos + 1796 <= len(data):
        h = data[pos:pos + 1796]
        pos += 1796
        be = lambda o: struct.unpack(">I", h[o:o + 4])[0]     # noqa: E731
        w, ht, bpc, bpp, bpl, cspace = be(372), be(376), be(384), be(388), be(392), be(400)
        hres, vres = be(276) or 300, be(280) or 300
        pw, ph = be(352), be(356)
        px = max(1, bpp // 8)
        rows = bytearray()
        y = 0
        while y < ht and pos < len(data):
            repeat = data[pos] + 1
            pos += 1
            line = bytearray()
            while len(line) < bpl and pos < len(data):
                c = data[pos]
                pos += 1
                if c <= 127:                                  # repeat one pixel c+1 times
                    line += data[pos:pos + px] * (c + 1)
                    pos += px
                elif c == 128:                                # fill the rest of the line with white
                    line += b"\xff" * (bpl - len(line))
                else:                                         # 257-c literal pixels
                    n = (257 - c) * px
                    line += data[pos:pos + n]
                    pos += n
            line = bytes(line[:bpl]).ljust(bpl, b"\xff")
            rows += line * min(repeat, ht - y)
            y += repeat
        mode = {1: "L", 3: "RGB"}.get(px, "RGB")
        img = Image.frombytes(mode, (w, ht), bytes(rows[:w * ht * px]).ljust(w * ht * px, b"\xff"), "raw", mode, bpl, 1) \
            if bpl == w * px else Image.frombytes(mode, (w, ht), b"".join(bytes(rows[i * bpl:i * bpl + w * px]) for i in range(ht)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92, dpi=(hres, vres))
        rect = fitz.Rect(0, 0, pw or w * 72.0 / hres, ph or ht * 72.0 / vres)
        page = doc.new_page(width=rect.width, height=rect.height)
        page.insert_image(rect, stream=buf.getvalue())
    if doc.page_count == 0:
        raise ValueError("no pages in PWG raster")
    doc.save(out_path)
    doc.close()
    return out_path
