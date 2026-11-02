#!/usr/bin/env python3
"""proxylink – kleine Link-Startseite, die HTTP-Proxy-Regeln aus Zoraxy zieht.

Nur Python-Standardbibliothek. Konfiguration über Umgebungsvariablen (siehe README).
"""
import http.cookiejar
import json
import logging
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from sources import Proxmox, parse_docker_hosts

log = logging.getLogger("proxylink")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

ZORAXY_URL = os.getenv("ZORAXY_URL", "http://zoraxy:8000").rstrip("/")
ZORAXY_USER = os.getenv("ZORAXY_USER", "")
ZORAXY_PASS = os.getenv("ZORAXY_PASS", "")
ZORAXY_INSECURE = os.getenv("ZORAXY_INSECURE", "false").lower() in ("1", "true", "yes")
REFRESH = int(os.getenv("REFRESH_SECONDS", "60"))
LINK_SCHEME = os.getenv("LINK_SCHEME", "https")          # Schema für die erzeugten Links
TITLE = os.getenv("TITLE", "proxylink")
SHOW_DISABLED = os.getenv("SHOW_DISABLED", "false").lower() in ("1", "true", "yes")
EXCLUDE = [p.strip().lower() for p in os.getenv("EXCLUDE", "").split(",") if p.strip()]
OVERRIDES_FILE = os.getenv("OVERRIDES_FILE", "/config/overrides.json")
PORT = int(os.getenv("PORT", "8080"))
PVE_URL = os.getenv("PVE_URL", "")                       # z. B. https://192.168.10.2:8006
PVE_TOKEN = os.getenv("PVE_TOKEN", "")                   # home@pve!dash=xxxxxxxx-...
PVE_INSECURE = os.getenv("PVE_INSECURE", "true").lower() in ("1", "true", "yes")
DOCKER_HOSTS = os.getenv("DOCKER_HOSTS", "")             # name=url[@ip];name2=url2
CHECK_ORIGINS = os.getenv("CHECK_ORIGINS", "true").lower() in ("1", "true", "yes")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class ZoraxyClient:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if ZORAXY_INSECURE:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.logged_in = False

    def _req(self, path, data=None, headers=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(ZORAXY_URL + path, data=body, headers=headers or {})
        with self.opener.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def _csrf_token(self):
        """Zoraxy 3.x schützt POST-Requests per CSRF; der Token steht als <meta> in der Login-Seite."""
        for page in ("/login.html", "/"):
            try:
                _, html = self._req(page)
            except Exception:
                continue
            m = re.search(r'<meta[^>]*name="[^"]*csrf[^"]*"[^>]*content="([^"]+)"', html, re.I) or \
                re.search(r'<meta[^>]*content="([^"]+)"[^>]*name="[^"]*csrf[^"]*"', html, re.I)
            if m:
                return m.group(1)
        return None

    def login(self):
        if not ZORAXY_USER:
            # Zoraxy im -noauth Modus
            self.logged_in = True
            return
        self.jar.clear()
        token = self._csrf_token()
        headers = {"X-CSRF-Token": token} if token else {}
        status, body = self._req("/api/auth/login",
                                 {"username": ZORAXY_USER, "password": ZORAXY_PASS},
                                 headers)
        if "error" in body.lower():
            raise RuntimeError(f"Login fehlgeschlagen: {body.strip()[:200]}")
        self.logged_in = True
        log.info("Bei Zoraxy eingeloggt (%s)", ZORAXY_URL)

    def proxy_hosts(self):
        if not self.logged_in:
            self.login()
        try:
            _, body = self._req("/api/proxy/list?type=host")
            data = json.loads(body)
        except (json.JSONDecodeError, urllib.error.HTTPError):
            # Session abgelaufen -> Login-Seite/401 statt JSON
            self.login()
            _, body = self._req("/api/proxy/list?type=host")
            data = json.loads(body)
        if isinstance(data, dict) and "error" in data:
            self.logged_in = False
            raise RuntimeError(data["error"])
        return data or []


def load_overrides():
    try:
        with open(OVERRIDES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("overrides.json nicht lesbar: %s", e)
        return {}


def to_links(hosts):
    ov = load_overrides()
    links = []
    for h in hosts:
        domain = (h.get("RootOrMatchingDomain") or "").strip()
        if not domain:
            continue
        disabled = bool(h.get("Disabled"))
        if disabled and not SHOW_DISABLED:
            continue
        if any(x in domain.lower() for x in EXCLUDE):
            continue
        o = ov.get(domain, {})
        if o.get("hide"):
            continue
        # Wildcards wie *.example.com sind keine klickbaren Links
        if "*" in domain:
            continue
        raw = h.get("ActiveOrigins") or []
        origins = [{"addr": x.get("OriginIpOrDomain", ""), "tls": bool(x.get("RequireTLS"))} for x in raw]
        if not origins and h.get("Domain"):  # ältere Zoraxy-Versionen
            origins = [{"addr": h["Domain"], "tls": bool(h.get("RequireTLS"))}]
        tags = h.get("Tags") or []
        links.append({
            "domain": domain,
            "name": o.get("name") or domain.split(".")[0].replace("-", " ").title(),
            "url": o.get("url") or f"{LINK_SCHEME}://{domain}",
            "group": o.get("group") or (tags[0] if tags else "Allgemein"),
            "icon": o.get("icon", ""),
            "description": o.get("description", ""),
            "origins": origins,
            "aliases": h.get("MatchingDomainAlias") or [],
            "disabled": disabled,
        })
    links.sort(key=lambda l: (l["group"].lower(), l["name"].lower()))
    return links


# --------------------------------------------------------------------------- Routen verfolgen
_dns = {}


def resolve(host):
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass
    if host not in _dns:
        try:
            _dns[host] = socket.gethostbyname(host)
        except OSError:
            _dns[host] = None
    return _dns[host]


def split_addr(addr, tls):
    a = re.sub(r"^\w+://", "", addr).split("/")[0]
    if a.startswith("["):  # IPv6
        host, _, port = a[1:].partition("]")
        port = port.lstrip(":")
    else:
        host, _, port = a.rpartition(":") if a.count(":") == 1 else (a, "", "")
    return host, int(port) if port.isdigit() else (443 if tls else 80)


def tcp_ok(host, port):
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True
    except OSError:
        return False


def trace(links, guests, containers):
    """Ordnet jedem Origin einen Proxmox-Gast und/oder Docker-Container zu."""
    def guest_by_ip(ip):
        return next((g for g in guests if ip in g["ips"]), None)

    def trace_origin(o):
        host, port = split_addr(o["addr"], o["tls"])
        ip = resolve(host)
        cont = guest = None
        # 1) Hostname = Containername (gleiches Docker-Netz wie Zoraxy)
        cont = next((c for c in containers if c["name"].lower() == host.lower()), None)
        if ip:
            # 2) veröffentlichter Port auf einem Docker-Host
            cont = cont or next((c for c in containers if c["host_ip"] == ip and port in c["ports"]), None)
            # 3) Container-IP direkt (macvlan/ipvlan oder Bridge)
            cont = cont or next((c for c in containers if ip in c["ips"]), None)
            guest = guest_by_ip(ip)
        # Container läuft auf einem Docker-Host, der selbst ein PVE-Gast ist
        if cont and not guest and cont["host_ip"]:
            guest = guest_by_ip(cont["host_ip"])
        # 4) Fallback: Hostname = Gastname
        if not guest:
            short = host.split(".")[0].lower()
            guest = next((g for g in guests if g["name"].lower() == short), None)
        reachable = tcp_ok(ip or host, port) if CHECK_ORIGINS and (ip or not cont) else None
        if cont:
            cont["routed"] = True
        if guest:
            guest["routed"] = True
        return {
            **o, "host": host, "ip": ip, "port": port, "reachable": reachable,
            "guest": guest and {k: guest[k] for k in ("kind", "vmid", "name", "node", "status")},
            "container": cont and {k: cont[k] for k in ("host", "name", "state", "status", "image")},
        }

    for g in guests:
        g["routed"] = False
    for c in containers:
        c["routed"] = False
    with ThreadPoolExecutor(16) as ex:
        for l in links:
            l["targets"] = list(ex.map(trace_origin, l["origins"]))
            states = []
            for t in l["targets"]:
                if t["reachable"] is False or (t["container"] and t["container"]["state"] != "running") \
                        or (t["guest"] and t["guest"]["status"] != "running"):
                    states.append("down")
                elif t["reachable"] or t["container"] or t["guest"]:
                    states.append("up")
                else:
                    states.append("unknown")
            l["state"] = "down" if states and all(s == "down" for s in states) else \
                         "warn" if "down" in states else ("up" if "up" in states else "unknown")
            l["matched"] = any(t["guest"] or t["container"] for t in l["targets"])


class Cache:
    def __init__(self):
        self.client = ZoraxyClient()
        self.pve = Proxmox(PVE_URL, PVE_TOKEN, PVE_INSECURE) if PVE_URL and PVE_TOKEN else None
        self.dockers = parse_docker_hosts(DOCKER_HOSTS)
        self.links, self.guests, self.containers = [], [], []
        self.error, self.warnings, self.updated = None, [], 0
        self.lock = threading.Lock()

    def _inventory(self):
        """Proxmox-Gäste und Docker-Container parallel holen; Fehler einzelner Quellen sind nur Warnungen."""
        warnings, guests, containers = [], [], []
        jobs = {}
        with ThreadPoolExecutor(8) as ex:
            if self.pve:
                jobs[ex.submit(self.pve.guests)] = "Proxmox"
            for d in self.dockers:
                jobs[ex.submit(d.containers)] = "Docker " + d.name
            for fut, name in jobs.items():
                try:
                    res = fut.result()
                    (guests if name == "Proxmox" else containers).extend(res)
                except Exception as e:
                    log.warning("%s nicht abrufbar: %s", name, e)
                    warnings.append(f"{name}: {e}")
        return guests, containers, warnings

    def refresh(self):
        try:
            links = to_links(self.client.proxy_hosts())
            error = None
        except Exception as e:
            log.error("Zoraxy-Abruf fehlgeschlagen: %s", e)
            links, error = None, str(e)
        guests, containers, warnings = self._inventory()
        if links is not None:
            trace(links, guests, containers)
        with self.lock:
            if links is not None:
                self.links = links
            self.guests, self.containers = guests, containers
            self.error, self.warnings, self.updated = error, warnings, time.time()
        log.info("%d Links, %d Gäste, %d Container", len(self.links), len(guests), len(containers))

    def loop(self):
        while True:
            self.refresh()
            time.sleep(REFRESH)

    def snapshot(self):
        with self.lock:
            return {"title": TITLE, "links": self.links, "error": self.error, "updated": self.updated,
                    "warnings": self.warnings, "guests": self.guests, "containers": self.containers,
                    "sources": {"proxmox": bool(self.pve), "docker": [d.name for d in self.dockers]}}


CACHE = Cache()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC_DIR, **kw)

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/links":
            return self._json(CACHE.snapshot())
        if self.path == "/api/refresh":
            CACHE.refresh()
            return self._json(CACHE.snapshot())
        if self.path == "/health":
            return self._json({"ok": CACHE.error is None})
        return super().do_GET()


if __name__ == "__main__":
    threading.Thread(target=CACHE.loop, daemon=True).start()
    log.info("proxylink läuft auf Port %d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
