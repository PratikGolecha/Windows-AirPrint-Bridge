"""
Web admin page for AirPrint Bridge (served by the bridge's own HTTP port, e.g. http://192.168.0.23:631/).

Read-only information (status, jobs, capabilities) is open to the LAN, like printing itself.  Everything that
changes something, plus the log and the configuration, needs the admin PIN (``admin_pin`` in config.json; a random
6-digit PIN is created there the first time the bridge starts, it is never written to the log).

The page is one self-contained HTML document (below); all data comes from the JSON API under /api/.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Optional

logger = logging.getLogger("airprint_bridge")

VERSION = "phase-1"
_started = time.time()
app = None                       # the airprint_bridge module (set by attach())
_pin = ""
_fail = {"n": 0, "until": 0.0}
_JOB_STATE = {3: "pending", 5: "printing", 6: "stopped", 7: "canceled", 8: "aborted", 9: "completed"}


def attach(bridge_module) -> None:
    """Called once at start-up with the airprint_bridge module; makes sure an admin PIN exists."""
    global app, _pin
    app = bridge_module
    cfg = app._load_config()
    _pin = str(cfg.get("admin_pin") or "")
    if not _pin:
        _pin = "".join(secrets.choice("0123456789") for _ in range(6))
        try:
            cfg["admin_pin"] = _pin
            _write_config(cfg)
            logger.info("Web UI: an admin PIN was created in config.json (key admin_pin)")
        except Exception:  # noqa: BLE001
            logger.exception("could not store the admin PIN - actions in the web UI are disabled")
            _pin = ""


def _config_path():
    return app._app_dir / "config.json"


def _write_config(cfg: dict) -> None:
    path = _config_path()
    if path.exists():
        try:
            (app._app_dir / "config.json.bak").write_bytes(path.read_bytes())
        except OSError:
            pass
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _ctx(pid: str):
    return app.IPPRequestHandler.contexts.get(pid)


# --------------------------------------------------------------------------- data


def _iso(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def _printer_info(ctx) -> dict:
    jt = app.job_tracking
    st = jt.printer_status(ctx.printer_name, max_age=1.0)
    caps = app.driver_caps.peek(ctx.printer_name)
    info = {
        "id": ctx.id, "name": ctx.printer_name, "display": app.get_display_name(ctx.printer_name, ctx.cfg),
        "ip": ctx.ip, "state": {3: "idle", 4: "printing", 5: "stopped"}.get(st["state"], "?"),
        "reasons": st["reasons"], "queued": st["queued"], "color": app.printer_is_color(ctx.printer_name, ctx.cfg),
        "paper_default": (app.get_default_paper(ctx.cfg) or ("", ""))[0],
        "media": app.supported_media(ctx.printer_name, ctx.cfg, (app.get_default_paper(ctx.cfg) or ("iso_a4_210x297mm",))[0])[:60],
        "wsd": bool(ctx.wsd), "location": ctx.cfg.get("location", ""),
        "urls": {"ipp": f"ipp://{ctx.ip}:{app.IPP_PORT}{'' if ctx is app.IPPRequestHandler.default_ctx else '/' + ctx.id}/ipp/print"},
        "caps": caps.summary() if caps else None,
        "media_types": [{"keyword": m["keyword"], "name": m["display"], "borderless": m["borderless"]} for m in caps.media_types()] if caps else [],
        "trays": [{"keyword": b["keyword"], "name": b["display"]} for b in caps.input_bins()] if caps else [],
        "sides": sorted(caps.duplex_options()) if caps else [],
        "quality": sorted(caps.quality_options()) if caps else [3, 4, 5],
        "borderless": bool(caps and caps.borderless_supported()),
        "scanner": None,
        "supplies": app.supplies.get(ctx.cfg),
        "maintenance": app.maintenance.actions(ctx.printer_name, bool(ctx.cfg.get("maintenance_advanced"))),
    }
    sc = ctx.escl
    if sc is not None:
        try:
            info["scanner"] = {"name": sc.wia_name, "adf": sc.has_adf(), "duplex": sc.has_duplex(),
                               "resolutions": sc.resolutions(), "busy": bool(app.escl_scanner.SCAN_LOCK.locked())}
        except Exception:  # noqa: BLE001
            info["scanner"] = {"name": sc.wia_name, "error": True}
    return info


def status() -> dict:
    log_kb = 0
    try:
        log_kb = int(os.path.getsize(app.LOG_FILE) / 1024)
    except OSError:
        pass
    return {"host": os.environ.get("COMPUTERNAME", ""), "ip": app.IPPRequestHandler.host_ip, "version": VERSION,
            "uptime": int(time.time() - _started), "log_kb": log_kb, "pin_set": bool(_pin),
            "printers": [_printer_info(c) for c in app.IPPRequestHandler.contexts.values()],
            "network": [{"name": n.get("name", n.get("ip", "")), "ip": n.get("ip", ""),
                         "supplies": app.supplies.get({"supplies_ip": n.get("ip", "")})}
                        for n in (app._load_config().get("network_printers") or []) if isinstance(n, dict)]}


def jobs() -> list:
    out = []
    for c in app.IPPRequestHandler.contexts.values():
        for j in app.JOBS.list(c.printer_name):
            out.append({"id": j["id"], "printer": c.id, "name": j["name"], "user": j["user"],
                        "state": _JOB_STATE.get(j["state"], str(j["state"])), "reasons": j["reasons"],
                        "created": _iso(j["created"]), "finished": _iso(j["finished"]), "active": j["state"] < 7})
    return sorted(out, key=lambda j: j["created"], reverse=True)[:100]


def spooler(ctx) -> list:
    import win32print
    h = win32print.OpenPrinter(ctx.printer_name)
    try:
        return [{"id": j["JobId"], "doc": j.get("pDocument", ""), "user": j.get("pUserName", ""),
                 "status": int(j.get("Status", 0)), "pages": j.get("TotalPages", 0), "printed": j.get("PagesPrinted", 0)}
                for j in win32print.EnumJobs(h, 0, 100, 1)]
    finally:
        win32print.ClosePrinter(h)


def _printer_control(ctx, action: str) -> str:
    import win32print
    code = {"pause": 1, "resume": 2, "purge": 3}[action]
    h = win32print.OpenPrinter(ctx.printer_name, {"DesiredAccess": win32print.PRINTER_ALL_ACCESS})
    try:
        win32print.SetPrinter(h, 0, None, code)
    finally:
        win32print.ClosePrinter(h)
    return action + "d" if action != "purge" else "queue cleared"


def _test_pdf(ctx, opts: dict) -> str:
    """A one-page test sheet: name, date, colour bars, greys, fine text, frame, corner marks."""
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    W, H = page.rect.width, page.rect.height
    page.draw_rect(fitz.Rect(4, 4, W - 4, H - 4), color=(0, 0, 0), width=0.6)
    for x, y in ((0, 0), (W, 0), (0, H), (W, H)):
        page.draw_circle(fitz.Point(x, y), 14, color=(0, 0, 0), fill=(0, 0, 0))
    page.insert_text((50, 80), "Printer test page", fontsize=26, color=(0, 0, 0))
    page.insert_text((50, 108), app.get_display_name(ctx.printer_name, ctx.cfg), fontsize=15)
    page.insert_text((50, 130), datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "   " + ", ".join(
        f"{k}={v}" for k, v in opts.items() if v not in ("", None, False)), fontsize=8)
    bars = [(0, 1, 1), (1, 0, 1), (1, 1, 0), (0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
    for i, c in enumerate(bars):
        page.draw_rect(fitz.Rect(50 + i * 70, 160, 50 + i * 70 + 66, 230), color=None, fill=c)
    for i in range(16):
        g = i / 15
        page.draw_rect(fitz.Rect(50 + i * 31, 240, 50 + i * 31 + 30, 290), color=None, fill=(g, g, g))
    for i in range(50):
        r = i / 49
        page.draw_rect(fitz.Rect(50 + i * 10.4, 300, 50 + i * 10.4 + 10.4, 340), color=None, fill=(r, 0.4, 1 - r))
    y = 370
    for size in (6, 8, 10, 12, 16):
        page.insert_text((50, y), f"{size} pt - The quick brown fox jumps over the lazy dog 0123456789", fontsize=size)
        y += size + 8
    for i in range(0, 60, 2):                           # fine line pairs: shows the real resolution
        page.draw_line(fitz.Point(50 + i * 4, 480), fitz.Point(50 + i * 4, 560), color=(0, 0, 0), width=0.25 + i / 120)
    page.draw_rect(fitz.Rect(50, 590, W - 50, 780), color=(0.1, 0.3, 0.8), fill=(0.9, 0.94, 1), width=2)
    page.insert_text((70, 690), "Frame: should be complete on all four sides", fontsize=14)
    path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", prefix="airprint_test_").name
    doc.save(path)
    doc.close()
    return path


def _start_test(ctx, opts: dict) -> int:
    attrs = {"job-name": "Web UI test page", "requesting-user-name": "web-ui"}
    if opts.get("media_type"):
        attrs["media-type"] = opts["media_type"]
    if opts.get("quality") in ("3", "4", "5", 3, 4, 5):
        attrs["print-quality"] = str(opts["quality"])
    if opts.get("color") in ("color", "monochrome"):
        attrs["print-color-mode"] = opts["color"]
    if opts.get("sides") and opts["sides"] != "one-sided":
        attrs["sides"] = opts["sides"]
    if opts.get("media_source"):
        attrs["media-source"] = opts["media_source"]
    if opts.get("borderless"):
        for k in ("media-top-margin", "media-bottom-margin", "media-left-margin", "media-right-margin"):
            attrs[k] = "0"
    if opts.get("number_up"):
        attrs["number-up"] = str(int(opts["number_up"]))
    size, _ = app.choose_media_size(None, False, ctx.cfg)
    if opts.get("media"):
        size = app.IPP_MEDIA_SIZES.get(opts["media"]) or app.size_from_keyword(opts["media"]) or size
    copies = max(1, min(int(opts.get("copies") or 1), 20))
    path = _test_pdf(ctx, {k: v for k, v in opts.items() if k in ("media", "media_type", "quality", "color", "sides", "borderless", "number_up")})
    jid = app.JOBS.create(ctx.printer_name, attrs["job-name"], "web-ui")
    threading.Thread(target=app._run_ipp_job, name=f"ipp-job-{jid}", daemon=True,
                     args=(jid, ctx.printer_name, path, ".pdf", size, attrs, copies)).start()
    return jid


def _scan_test(ctx, dpi: int) -> tuple:
    sc = ctx.escl
    if sc is None:
        raise RuntimeError("no scanner is shared for this printer")
    res = sc.resolutions()
    dpi = min(res, key=lambda r: abs(r - dpi))
    jid = sc.create_job_settings({"dpi": dpi, "color": True, "format": "image/jpeg", "region": None, "source": "platen"})
    doc = sc.next_document(jid)
    if not doc:
        raise RuntimeError(sc.job_error(jid) or "the scan returned nothing")
    sc.delete_job(jid)
    return doc


def _restart() -> None:
    def go() -> None:
        time.sleep(1.5)
        try:
            subprocess.Popen(["cmd", "/c", 'ping -n 6 127.0.0.1 >nul & schtasks /Run /TN "AirPrint Bridge"'],
                             creationflags=0x00000008 | 0x08000000, close_fds=True)
        finally:
            os._exit(0)
    threading.Thread(target=go, daemon=True).start()


# --------------------------------------------------------------------------- HTTP glue


def is_ui_path(path: str) -> bool:
    return path in ("/", "/index.html", "/ui") or path.startswith("/api/")


def _auth(handler) -> bool:
    now = time.time()
    if now < _fail["until"]:
        return False
    given = handler.headers.get("X-Admin-Pin", "")
    ok = bool(_pin) and hmac.compare_digest(given.encode(), _pin.encode())
    if ok:
        _fail["n"] = 0
        return True
    if given:
        _fail["n"] += 1
        time.sleep(1.0)
        if _fail["n"] >= 5:
            _fail["until"] = now + 60
            _fail["n"] = 0
    return False


def _json(handler, obj, code: int = 200) -> None:
    handler._send_plain(code, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8",
                        {"Cache-Control": "no-store"})


def handle(handler, method: str, body: bytes = b"") -> None:
    """Serve the page and the JSON API.  ``handler`` is the IPPRequestHandler (path already routed)."""
    parsed = urllib.parse.urlparse(handler.path)
    path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
    try:
        if method == "GET" and path in ("/", "/index.html", "/ui"):
            handler._send_plain(200, PAGE.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            return
        parts = [p for p in path.split("/") if p]                 # api, ...
        if method == "GET":
            if parts[1:] == ["status"]:
                return _json(handler, status())
            if parts[1:] == ["jobs"]:
                return _json(handler, jobs())
            if parts[1] == "spooler" and len(parts) == 3:
                c = _ctx(parts[2])
                return _json(handler, spooler(c)) if c else _json(handler, {"error": "unknown printer"}, 404)
            # everything below needs the PIN
            if not _auth(handler):
                return _json(handler, {"error": "PIN required"}, 401)
            if parts[1:] == ["log"]:
                n = min(int((query.get("n") or ["300"])[0]), 3000)
                with open(app.LOG_FILE, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - 600000))
                    lines = fh.read().decode("utf-8", "replace").splitlines()[-n:]
                return _json(handler, {"lines": lines})
            if parts[1:] == ["config"]:
                cfg = dict(app._load_config())
                cfg["admin_pin"] = "(hidden)"
                return _json(handler, cfg)
            if parts[1] == "scan" and len(parts) == 3:
                c = _ctx(parts[2])
                data, mime = _scan_test(c, int((query.get("dpi") or ["75"])[0]))
                return handler._send_plain(200, data, mime, {"Cache-Control": "no-store"})
            return _json(handler, {"error": "not found"}, 404)

        if method == "POST":
            if not _auth(handler):
                return _json(handler, {"error": "PIN required (wrong PIN?)"}, 401)
            data = json.loads(body.decode("utf-8") or "{}") if body else {}
            if parts[1] == "jobs" and len(parts) == 4 and parts[3] == "cancel":
                jid = int(parts[2])
                j = app.JOBS.get(jid)
                ok = app.JOBS.cancel(jid)
                if ok and j:
                    app.job_tracking.delete_spooler_job(j["printer"], f"AirPrint job {jid}")
                return _json(handler, {"ok": ok})
            if parts[1] == "printers" and len(parts) == 4:
                c = _ctx(parts[2])
                if c is None:
                    return _json(handler, {"error": "unknown printer"}, 404)
                if parts[3] in ("pause", "resume", "purge"):
                    return _json(handler, {"ok": True, "message": _printer_control(c, parts[3])})
                if parts[3] == "testpage":
                    return _json(handler, {"ok": True, "job": _start_test(c, data)})
                if parts[3] == "maintenance":
                    act = str(data.get("action", ""))
                    if not app.job_tracking.printer_status(c.printer_name, max_age=0)["state"] == 3:
                        return _json(handler, {"error": "the printer is busy or has a problem - see Overview"}, 409)
                    msg = app.maintenance.run(c.printer_name, act, advanced=bool(c.cfg.get("maintenance_advanced")))
                    logger.info("Web UI maintenance: %s on %s", act, c.printer_name)
                    return _json(handler, {"ok": True, "message": msg})
                if parts[3] == "refresh-caps":
                    app.driver_caps.load(c.printer_name, refresh=True)
                    return _json(handler, {"ok": True})
            if parts[1] == "spooler" and len(parts) == 5 and parts[4] == "delete":
                c = _ctx(parts[2])
                import win32print
                h = win32print.OpenPrinter(c.printer_name, {"DesiredAccess": win32print.PRINTER_ALL_ACCESS})
                try:
                    win32print.SetJob(h, int(parts[3]), 0, None, win32print.JOB_CONTROL_DELETE)
                finally:
                    win32print.ClosePrinter(h)
                return _json(handler, {"ok": True})
            if parts[1:] == ["config"]:
                if not isinstance(data, dict) or not data:
                    return _json(handler, {"error": "config must be a JSON object"}, 400)
                if data.get("admin_pin") in (None, "", "(hidden)"):
                    data["admin_pin"] = _pin
                _write_config(data)
                return _json(handler, {"ok": True, "message": "saved - restart the bridge to apply"})
            if parts[1:] == ["restart"]:
                _restart()
                return _json(handler, {"ok": True, "message": "restarting - the page reconnects in about 20 seconds"})
        return _json(handler, {"error": "not found"}, 404)
    except (BrokenPipeError, ConnectionResetError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("web UI request %s %s failed", method, path)
        try:
            _json(handler, {"error": str(exc)[:300]}, 500)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- the page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Printer bridge</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--mut:#6b7686;--line:#dde2ea;--acc:#1f6feb;--ok:#1a7f37;--warn:#b26a00;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1c2027;--fg:#e6e9ef;--mut:#95a0b1;--line:#2c333d;--acc:#5b9dff;--ok:#4cc26a;--warn:#e0a03a;--bad:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0}header .sp{flex:1}
nav{display:flex;gap:4px;padding:8px 12px;overflow-x:auto;background:var(--card);border-bottom:1px solid var(--line)}
nav button{border:0;background:none;color:var(--mut);padding:8px 12px;border-radius:8px;font:inherit;cursor:pointer;white-space:nowrap}
nav button.on{background:var(--acc);color:#fff}
main{max-width:1000px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.card h2{margin:0 0 8px;font-size:16px}.row{display:flex;flex-wrap:wrap;gap:8px 18px}.row>div{min-width:150px}
.k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok);border-color:var(--ok)}.warn{color:var(--warn);border-color:var(--warn)}.bad{color:var(--bad);border-color:var(--bad)}
button.b,select,input,textarea{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
button.b{cursor:pointer;background:var(--acc);color:#fff;border-color:var(--acc)}button.b.s{background:none;color:var(--acc)}button.b.d{background:var(--bad);border-color:var(--bad)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px}
textarea{width:100%;min-height:340px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;max-height:520px;font-size:12px}
label{display:inline-flex;gap:6px;align-items:center;margin:4px 12px 4px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.msg{padding:8px 12px;border-radius:8px;margin-bottom:10px;background:var(--bg);border:1px solid var(--line)}
img.prev{max-width:100%;border:1px solid var(--line);border-radius:8px}
</style></head><body>
<header><h1>Printer bridge <span id="host" class="k"></span></h1><span class="sp"></span>
<span id="pinstate" class="k"></span><button class="b s" onclick="setPin()">Admin PIN</button></header>
<nav id="nav"></nav><main id="main"></main>
<script>
const TABS=['Overview','Jobs','Test print','Scanner','Maintenance','Settings','Log'];let tab=0,S=null,J=[],msg='',busy=false;
const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pin=()=>{try{return sessionStorage.getItem('pin')||''}catch(e){return ''}};
function setPin(){const p=prompt('Admin PIN (from config.json, key admin_pin)',pin());if(p!==null){try{sessionStorage.setItem('pin',p)}catch(e){}render()}}
async function api(path,opt={}){const h={'X-Admin-Pin':pin()};if(opt.body)h['Content-Type']='application/json';
 const r=await fetch(path,{method:opt.method||'GET',headers:h,body:opt.body?JSON.stringify(opt.body):undefined});
 if(r.status==401){throw new Error('Wrong or missing admin PIN - press "Admin PIN"')}
 if((r.headers.get('content-type')||'').startsWith('image/'))return r.blob();
 const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
async function act(path,body,ok){try{const j=await api(path,{method:'POST',body:body||{}});msg=ok||j.message||'Done';}catch(e){msg='Error: '+e.message}await refresh();}
async function refresh(){try{S=await api('/api/status');J=await api('/api/jobs')}catch(e){msg='Cannot reach the bridge ('+e.message+')'}render()}
function pill(t,c){return `<span class="pill ${c}">${esc(t)}</span>`}
function stateP(p){const c=p.state=='idle'?'ok':p.state=='printing'?'warn':'bad';return pill(p.state,c)+(p.reasons&&p.reasons[0]!='none'?' '+p.reasons.map(r=>pill(r,'warn')).join(' '):'')}
function up(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+'h '+m+'m'}
function overview(){return S.printers.map(p=>`<div class="card"><h2>${esc(p.display)} ${stateP(p)}</h2>
<div class="row"><div><div class="k">Queue</div>${esc(p.name)}</div><div><div class="k">Address</div>${esc(p.ip)}</div>
<div><div class="k">Waiting jobs</div>${p.queued}</div><div><div class="k">Colour</div>${p.color?'yes':'no'}</div>
<div><div class="k">Default paper</div>${esc(p.paper_default||'printer default')}</div><div><div class="k">Windows add-device</div>${p.wsd?'on':'off'}</div></div>
<p class="k" style="margin:12px 0 4px">What the driver offers</p>
<div class="row"><div><b>Paper types</b><br>${p.media_types.map(m=>esc(m.name||m.keyword)).join(', ')||'-'}</div>
<div><b>Two-sided</b><br>${p.sides.length>1?p.sides.join(', '):'not automatic'}</div>
<div><b>Trays</b><br>${p.trays.length>1?p.trays.map(t=>esc(t.name||t.keyword)).join(', '):'single'}</div>
<div><b>Borderless</b><br>${p.borderless?'yes':'no'}</div><div><b>Quality</b><br>${p.quality.map(q=>({3:'draft',4:'normal',5:'high'}[q])).join(', ')}</div></div>
<p class="k" style="margin:12px 0 4px">Actions</p>
<button class="b s" onclick="act('/api/printers/${p.id}/pause')">Pause queue</button>
<button class="b s" onclick="act('/api/printers/${p.id}/resume')">Resume queue</button>
<button class="b s" onclick="if(confirm('Delete ALL waiting jobs of this printer?'))act('/api/printers/${p.id}/purge')">Clear queue</button>
<button class="b s" onclick="act('/api/printers/${p.id}/refresh-caps',{},'Driver features re-read')">Re-read driver features</button>
<p class="k" style="margin:12px 0 4px">Add on a phone / Mac (address)</p><code>${esc(p.urls.ipp)}</code></div>`).join('')}
function jobs(){return `<div class="card"><h2>Jobs sent through the bridge</h2><table><tr><th>#</th><th>Printer</th><th>Document</th><th>User</th><th>State</th><th>When</th><th></th></tr>
${J.map(j=>`<tr><td>${j.id}</td><td>${esc(j.printer)}</td><td>${esc(j.name)}</td><td>${esc(j.user)}</td><td>${pill(j.state,j.active?'warn':j.state=='completed'?'ok':'bad')}</td><td>${esc(j.created)}</td>
<td>${j.active?`<button class="b s d" onclick="act('/api/jobs/${j.id}/cancel')">Cancel</button>`:''}</td></tr>`).join('')||'<tr><td colspan=7>No jobs yet since the last start.</td></tr>'}</table>
<p class="k">Jobs sent from a Windows PC directly to the queue are in that PC's own queue window.</p></div>`}
function testp(){const p=S.printers;return p.map(x=>`<div class="card"><h2>Test page - ${esc(x.display)}</h2>
<div class="grid">
<label>Paper type<select id="mt_${x.id}"><option value="">(driver default)</option>${x.media_types.map(m=>`<option value="${esc(m.keyword)}">${esc(m.name||m.keyword)}</option>`).join('')}</select></label>
<label>Size<select id="sz_${x.id}"><option value="">(default)</option>${x.media.map(m=>`<option>${esc(m)}</option>`).join('')}</select></label>
<label>Quality<select id="q_${x.id}"><option value="">normal</option>${x.quality.map(q=>`<option value="${q}">${{3:'draft',4:'normal',5:'high'}[q]}</option>`).join('')}</select></label>
<label>Colour<select id="c_${x.id}"><option value="">auto</option><option>color</option><option>monochrome</option></select></label>
<label>Sides<select id="sd_${x.id}">${(x.sides.length?x.sides:['one-sided']).map(s=>`<option>${s}</option>`).join('')}</select></label>
<label>Tray<select id="tr_${x.id}"><option value="">auto</option>${x.trays.map(t=>`<option value="${esc(t.keyword)}">${esc(t.name||t.keyword)}</option>`).join('')}</select></label>
<label>Copies<input id="cp_${x.id}" type="number" min="1" max="20" value="1" style="width:70px"></label>
<label>Per sheet<select id="nu_${x.id}"><option>1</option><option>2</option><option>4</option></select></label></div>
<label><input type="checkbox" id="bl_${x.id}" ${x.borderless?'':'disabled'}> Borderless</label>
<p><button class="b" onclick="testPrint('${x.id}')">Print test page</button> <span class="k">uses one sheet of paper</span></p></div>`).join('')}
function testPrint(id){const v=k=>$('#'+k+'_'+id).value;act('/api/printers/'+id+'/testpage',{media_type:v('mt'),media:v('sz'),quality:v('q'),color:v('c'),sides:v('sd'),media_source:v('tr'),
 copies:+v('cp'),number_up:+v('nu'),borderless:$('#bl_'+id).checked},'Test page sent - see Jobs')}
function scanner(){return S.printers.map(x=>`<div class="card"><h2>Scanner - ${esc(x.display)}</h2>${x.scanner?`<div class="row"><div><div class="k">Device</div>${esc(x.scanner.name)}</div>
<div><div class="k">Feeder</div>${x.scanner.adf?'yes':'no'}</div><div><div class="k">Feeder two-sided</div>${x.scanner.duplex?'yes':'no'}</div>
<div><div class="k">Resolutions (dpi)</div>${(x.scanner.resolutions||[]).join(', ')}</div><div><div class="k">State</div>${x.scanner.busy?pill('busy','warn'):pill('ready','ok')}</div></div>
<p><button class="b" onclick="scanTest('${x.id}')">Test scan (glass, 75 dpi)</button></p><div id="sc_${x.id}"></div>`:'No scanner is shared for this printer.'}</div>`).join('')}
async function scanTest(id){const el=$('#sc_'+id);el.textContent='Scanning...';try{const b=await api('/api/scan/'+id+'?dpi=75');el.innerHTML='<img class="prev" src="'+URL.createObjectURL(b)+'">'}catch(e){el.textContent='Error: '+e.message}}
function netcards(){return (S.network||[]).map(n=>`<div class="card"><h2>${esc(n.name)} <span class="k">${esc(n.ip)} \u00b7 network printer</span></h2>${n.supplies.available?n.supplies.items.map(i=>`<div style="margin:4px 0"><span style="display:inline-block;width:150px">${esc(i.name)}</span><progress max="100" value="${Math.max(0,i.level)}"></progress> ${i.level>=0?i.level+'%':'unknown'}</div>`).join(''):esc(n.supplies.note)}</div>`).join('')}
const COST=['no ink','small amount of ink','a lot of ink','a lot of ink'];
function maintAct(id,a){if(confirm(a.label+'\n\n'+a.desc+'\n\nUses: '+COST[a.cost]+'.\nContinue?'))act('/api/printers/'+id+'/maintenance',{action:a.id},a.label+' sent - watch the printer')}
let MA={};
function maint(){MA={};return netcards()+S.printers.map(x=>{x.maintenance.forEach(a=>MA[x.id+'|'+a.id]=a);return `<div class="card"><h2>Maintenance - ${esc(x.display)}</h2>
<p>Status: ${stateP(x)}</p><p class="k">Ink / toner</p>
${x.supplies.available?x.supplies.items.map(i=>`<div style="margin:4px 0"><span style="display:inline-block;width:150px">${esc(i.name)}</span><progress max="100" value="${Math.max(0,i.level)}"></progress> ${i.level>=0?i.level+'%':'unknown'}</div>`).join(''):`<p>${esc(x.supplies.note)}</p>`}
<p class="k">Printer actions (sent to the printer exactly like the driver's Maintenance tab does)</p>
${x.maintenance.length?x.maintenance.map(a=>`<p><button class="b s" onclick="maintAct('${x.id}',MA['${x.id}|${a.id}'])">${esc(a.label)}</button> <span class="k">${esc(a.desc)}</span></p>`).join(''):'<p class="k">No maintenance commands were found in this printer\'s driver. Use the printer\'s own panel or web page.</p>'}</div>`}).join('')}
let CFG='';async function settings(){if(!CFG){try{CFG=JSON.stringify(await api('/api/config'),null,2)}catch(e){return `<div class="card">${esc(e.message)}</div>`}}
 return `<div class="card"><h2>Settings (config.json)</h2><textarea id="cfg" spellcheck="false">${esc(CFG)}</textarea>
<p><button class="b" onclick="saveCfg()">Save</button> <button class="b s" onclick="if(confirm('Restart the bridge now?'))act('/api/restart')">Restart bridge</button> <span class="k">changes apply after a restart</span></p></div>`}
async function saveCfg(){try{const o=JSON.parse($('#cfg').value);CFG='';await act('/api/config',o)}catch(e){msg='Not valid JSON: '+e.message;render()}}
let LOG=null;async function logv(){if(!LOG){try{LOG=(await api('/api/log?n=400')).lines.join('\n')}catch(e){return `<div class="card">${esc(e.message)}</div>`}}
 return `<div class="card"><h2>Log (last lines)</h2><button class="b s" onclick="LOG=null;render()">Reload</button><pre>${esc(LOG)}</pre></div>`}
async function render(){if(!S)return;$('#host').textContent=S.host+' \u00b7 '+S.ip+' \u00b7 up '+up(S.uptime);
 $('#pinstate').textContent=pin()?'PIN entered':'';$('#nav').innerHTML=TABS.map((t,i)=>`<button class="${i==tab?'on':''}" onclick="tab=${i};LOG=null;CFG='';render()">${t}</button>`).join('');
 const body=await ([overview,jobs,testp,scanner,maint,settings,logv][tab])();
 $('#main').innerHTML=(msg?`<div class="msg">${esc(msg)}</div>`:'')+body;}
refresh();setInterval(()=>{if(tab<2||tab==3)refresh()},5000);
</script></body></html>
"""
