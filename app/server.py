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
import uuid
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from sources import parse_docker_hosts, parse_proxmox
from monitor import Monitor, telegram, save_settings, public_settings, load_settings

log = logging.getLogger("proxylink")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

ZORAXY_URL = os.getenv("ZORAXY_URL", "http://192.168.60.199:8000").rstrip("/")
ZORAXY_USER = os.getenv("ZORAXY_USER", "")
ZORAXY_PASS = os.getenv("ZORAXY_PASS", "")
ZORAXY_INSECURE = os.getenv("ZORAXY_INSECURE", "false").lower() in ("1", "true", "yes")
REFRESH = int(os.getenv("REFRESH_SECONDS", "60"))
LINK_SCHEME = os.getenv("LINK_SCHEME", "https")          # Schema für die erzeugten Links
TITLE = os.getenv("TITLE", "proxylink")
SHOW_DISABLED = os.getenv("SHOW_DISABLED", "true").lower() in ("1", "true", "yes")
EXCLUDE = [p.strip().lower() for p in os.getenv("EXCLUDE", "").split(",") if p.strip()]
OVERRIDES_FILE = os.getenv("OVERRIDES_FILE", "/config/overrides.json")
PORT = int(os.getenv("PORT", "8080"))
DOCKER_HOSTS = os.getenv("DOCKER_HOSTS", "")             # name=url[@ip];name2=url2
CHECK_ORIGINS = os.getenv("CHECK_ORIGINS", "true").lower() in ("1", "true", "yes")
DATA_DIR = os.getenv("DATA_DIR", "/data")                # eigene Einträge (custom.json)
EDIT_TOKEN = os.getenv("EDIT_TOKEN", "")                 # optional: Schutz für Hinzufügen/Löschen
SHOW_UNPROXIED = os.getenv("SHOW_UNPROXIED", "true").lower() in ("1", "true", "yes")
PROBE_PORTS = [int(p) for p in os.getenv(
    "PROBE_PORTS", "80,443,3000,5000,5001,8000,8006,8080,8081,8096,8123,8443,9000,9443").split(",") if p.strip()]
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


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.warning("%s nicht lesbar: %s", path, e)
        return default


def load_overrides():
    """/config/overrides.json (Datei) + /data/overrides.json (per UI bearbeitet, hat Vorrang)."""
    ov = {k: dict(v) for k, v in _read_json(OVERRIDES_FILE, {}).items()}
    for k, v in _read_json(os.path.join(DATA_DIR, "overrides.json"), {}).items():
        ov.setdefault(k, {}).update(v)
    return ov


OVERRIDE_FIELDS = ("name", "icon", "group", "description", "url")
_ov_lock = threading.Lock()


def save_override(data):
    key = str(data.get("key", "")).strip()
    if not key:
        raise ValueError("key fehlt")
    e = {k: str(data.get(k, "")).strip()[:500] for k in OVERRIDE_FIELDS if str(data.get(k, "")).strip()}
    if data.get("hide"):
        e["hide"] = True
    path = os.path.join(DATA_DIR, "overrides.json")
    with _ov_lock:
        ov = _read_json(path, {})
        if e:
            ov[key] = e
        else:
            ov.pop(key, None)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(ov, f, indent=2, ensure_ascii=False)
        os.replace(path + ".tmp", path)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(path + ".tmp", path)


# --------------------------------------------------------------------------- Layout (Kategorien, Reihenfolge)
LAYOUT_FILE = os.path.join(DATA_DIR, "layout.json")
_layout_lock = threading.Lock()


def load_layout():
    lay = _read_json(LAYOUT_FILE, {})
    lay.setdefault("groups", [])
    lay.setdefault("order", [])
    st = lay.setdefault("status", {})
    st.setdefault("groups", [])
    st.setdefault("items", {})
    st.setdefault("order", [])
    return lay


def _clean_list(v, limit=500):
    return [str(x).strip()[:120] for x in (v or []) if str(x).strip()][:limit]


def save_layout(data):
    """Speichert Kategorien-Reihenfolge, Kachel-Reihenfolge, Zuordnungen und die Statusleisten-Gruppen.

    assign: {"o:<key>": "Gruppe", "c:<id>": "Gruppe"} – Zoraxy/ohne Proxy über Overrides, eigene Einträge direkt.
    """
    assign = {str(k): str(v).strip()[:120] for k, v in (data.get("assign") or {}).items() if str(v).strip()}
    ov_assign = {k[2:]: g for k, g in assign.items() if k.startswith("o:")}
    c_assign = {k[2:]: g for k, g in assign.items() if k.startswith("c:")}
    if ov_assign:
        path = os.path.join(DATA_DIR, "overrides.json")
        with _ov_lock:
            ov = _read_json(path, {})
            for key, g in ov_assign.items():
                ov.setdefault(key, {})["group"] = g
            _write_json(path, ov)
    if c_assign:
        with _custom_lock:
            items = load_custom()
            for it in items:
                if it["id"] in c_assign:
                    it["group"] = c_assign[it["id"]]
            save_custom(items)
    with _layout_lock:
        lay = load_layout()
        if "groups" in data:
            lay["groups"] = _clean_list(data["groups"])
        if "order" in data:
            lay["order"] = _clean_list(data["order"], 5000)
        if isinstance(data.get("status"), dict):
            st = data["status"]
            lay["status"] = {
                "groups": _clean_list(st.get("groups")),
                "items": {str(k): str(v).strip()[:120] for k, v in (st.get("items") or {}).items() if str(v).strip()},
                "order": _clean_list(st.get("order"), 1000),
            }
        _write_json(LAYOUT_FILE, lay)


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
        # Wildcards wie *.example.com sind keine klickbaren Links
        if "*" in domain:
            continue
        raw = h.get("ActiveOrigins") or []
        origins = [{"addr": x.get("OriginIpOrDomain", ""), "tls": bool(x.get("RequireTLS"))} for x in raw]
        if not origins and h.get("Domain"):  # ältere Zoraxy-Versionen
            origins = [{"addr": h["Domain"], "tls": bool(h.get("RequireTLS"))}]
        tags = h.get("Tags") or []
        links.append({
            "key": domain, "hidden": bool(o.get("hide")), "ov": o,
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


# --------------------------------------------------------------------------- eigene Einträge
CUSTOM_FILE = os.path.join(DATA_DIR, "custom.json")
_custom_lock = threading.Lock()
CUSTOM_FIELDS = ("name", "url", "group", "icon", "description", "origin")


def load_custom():
    try:
        with open(CUSTOM_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("custom.json nicht lesbar: %s", e)
        return []


def save_custom(items):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CUSTOM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CUSTOM_FILE)


def upsert_custom(entry):
    e = {k: str(entry.get(k, "")).strip()[:500] for k in CUSTOM_FIELDS}
    if not e["name"] or not e["url"]:
        raise ValueError("Name und URL sind Pflicht")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", e["url"], re.I):
        e["url"] = "http://" + e["url"]
    with _custom_lock:
        items = load_custom()
        cid = entry.get("id")
        for i, it in enumerate(items):
            if it["id"] == cid:
                items[i] = {"id": cid, **e}
                break
        else:
            items.append({"id": uuid.uuid4().hex[:10], **e})
        save_custom(items)


def delete_custom(cid):
    with _custom_lock:
        items = [i for i in load_custom() if i["id"] != cid]
        save_custom(items)


def custom_links():
    out = []
    for c in load_custom():
        p = urllib.parse.urlparse(c["url"])
        tls = p.scheme == "https"
        addr = c.get("origin") or (f"{p.hostname}:{p.port}" if p.port else (p.hostname or ""))
        out.append({
            "id": c["id"], "custom": True, "domain": p.hostname or c["url"],
            "name": c["name"], "url": c["url"], "group": c.get("group") or "Eigene",
            "icon": c.get("icon", ""), "description": c.get("description", ""),
            "origins": [{"addr": addr, "tls": tls}] if addr else [],
            "aliases": [], "disabled": False, "raw": c, "hidden": False,
        })
    return out


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


def tcp_ok(host, port, timeout=2):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
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
            "guest": guest and {k: guest[k] for k in ("cluster", "kind", "vmid", "name", "node", "status")},
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
            l["addrs"] = [f"{t['ip']}:{t['port']}" if t["ip"] else t["addr"] for t in l["targets"]]


HTTPS_PORTS = {443, 8006, 8443, 9443, 5001}


def discover(links, guests, containers):
    """Laufende Dienste ohne Proxy-Route: Container mit veröffentlichten Ports + offene Ports auf Gästen."""
    if not SHOW_UNPROXIED:
        return []
    ov = load_overrides()
    routed = {(t["ip"], t["port"]) for l in links for t in l.get("targets", []) if t.get("ip")}
    docker_ips = {c["host_ip"] for c in containers if c["host_ip"]}
    svc = []

    def card(name, ip, ports, guest=None, cont=None, desc=""):
        ports = [p for p in ports if (ip, p) not in routed]
        addrs = [f"{ip}:{p}" for p in ports]
        o = next((ov[k] for k in [name, *addrs] if k in ov), {})
        if not ports:
            return
        p = ports[0]
        svc.append({
            "unproxied": True, "key": name, "hidden": bool(o.get("hide")), "ov": o, "domain": addrs[0], "name": o.get("name") or name,
            "url": o.get("url") or f"{'https' if p in HTTPS_PORTS else 'http'}://{ip}:{p}",
            "group": o.get("group") or "Ohne Proxy", "icon": o.get("icon", ""),
            "description": o.get("description") or desc, "addrs": addrs, "origins": [],
            "targets": [{"guest": guest and {k: guest[k] for k in ("cluster", "kind", "vmid", "name", "node", "status")},
                         "container": cont and {k: cont[k] for k in ("host", "name", "state", "status", "image")}}],
            "state": "up", "matched": True, "disabled": False,
        })

    for c in containers:
        if c["state"] == "running" and not c["routed"] and c["ports"] and c["host_ip"]:
            g = next((g for g in guests if c["host_ip"] in g["ips"]), None)
            card(c["name"], c["host_ip"], c["ports"], guest=g, cont=c, desc=c["image"])

    cand = [g for g in guests if g["status"] == "running" and not g["routed"]
            and g["ips"] and not docker_ips.intersection(g["ips"])]
    jobs = [(g, ip, p) for g in cand for ip in g["ips"] if ":" not in ip for p in PROBE_PORTS]
    with ThreadPoolExecutor(64) as ex:
        res = list(ex.map(lambda j: tcp_ok(j[1], j[2], 0.8), jobs))
    open_ports = {}
    for (g, ip, p), ok in zip(jobs, res):
        if ok:
            open_ports.setdefault((g["vmid"], ip), (g, []))[1].append(p)
    for (_, ip), (g, ports) in open_ports.items():
        card(g["name"], ip, sorted(ports), guest=g)

    svc.sort(key=lambda l: l["name"].lower())
    return svc


class Cache:
    def __init__(self):
        self.client = ZoraxyClient()
        self.pves = parse_proxmox(os.environ)
        self.dockers = parse_docker_hosts(DOCKER_HOSTS)
        self.links, self.services, self.guests, self.containers = [], [], [], []
        self.refresh_lock = threading.Lock()
        self.error, self.warnings, self.updated = None, [], 0
        self.lock = threading.Lock()

    def _inventory(self):
        """Proxmox-Gäste und Docker-Container parallel holen; Fehler einzelner Quellen sind nur Warnungen."""
        warnings, guests, containers = [], [], []
        jobs = {}
        with ThreadPoolExecutor(8) as ex:
            for p in self.pves:
                jobs[ex.submit(p.guests)] = "Proxmox " + p.name
            for d in self.dockers:
                jobs[ex.submit(d.containers)] = "Docker " + d.name
            for fut, name in jobs.items():
                try:
                    res = fut.result()
                    (guests if name.startswith("Proxmox ") else containers).extend(res)
                except Exception as e:
                    log.warning("%s nicht abrufbar: %s", name, e)
                    warnings.append(f"{name}: {e}")
        return guests, containers, warnings

    def refresh(self):
        with self.refresh_lock:
            self._refresh()

    def _refresh(self):
        try:
            links = to_links(self.client.proxy_hosts())
            error = None
        except Exception as e:
            log.error("Zoraxy-Abruf fehlgeschlagen: %s", e)
            links, error = None, str(e)
        guests, containers, warnings = self._inventory()
        if links is None:
            links = [l for l in self.links if not l.get("custom")]  # letzten Zoraxy-Stand behalten
        links = sorted(links + custom_links(), key=lambda l: (l["group"].lower(), l["name"].lower()))
        trace(links, guests, containers)
        services = discover(links, guests, containers)
        with self.lock:
            self.links, self.services = links, services
            self.guests, self.containers = guests, containers
            self.error, self.warnings, self.updated = error, warnings, time.time()
        log.info("%d Links, %d ohne Proxy, %d Gäste, %d Container",
                 len(self.links), len(self.services), len(guests), len(containers))

    def apply_assign(self, assign):
        with self.lock:
            for l in self.links + self.services:
                k = ("c:" + l["id"]) if l.get("custom") else ("o:" + l.get("key", ""))
                if k in assign and str(assign[k]).strip():
                    l["group"] = str(assign[k]).strip()[:120]
                    if l.get("custom"):
                        l["raw"]["group"] = l["group"]
                    else:
                        l.setdefault("ov", {})["group"] = l["group"]

    def loop(self):
        while True:
            self.refresh()
            time.sleep(REFRESH)

    def snapshot(self):
        with self.lock:
            return {"title": TITLE, "links": self.links, "error": self.error, "updated": self.updated,
                    "warnings": self.warnings, "services": self.services,
                    "edit_protected": bool(EDIT_TOKEN), "layout": load_layout(), "zoraxy_url": ZORAXY_URL, "guests": self.guests, "containers": self.containers,
                    "sources": {"proxmox": [p.name for p in self.pves], "docker": [d.name for d in self.dockers]}}


CACHE = Cache()
MONITOR = Monitor(CACHE.pves)


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
        if self.path == "/api/status":
            return self._json(MONITOR.snapshot())
        if self.path == "/api/settings":
            return self._json(public_settings())
        if self.path == "/api/refresh":
            CACHE.refresh()
            return self._json(CACHE.snapshot())
        if self.path == "/health":
            return self._json({"ok": CACHE.error is None})
        return super().do_GET()

    def _authorized(self):
        if EDIT_TOKEN and self.headers.get("X-Edit-Token") != EDIT_TOKEN:
            self._json({"error": "Token falsch oder fehlt"}, 403)
            return False
        return True

    def do_POST(self):
        if self.path in ("/api/settings", "/api/telegram-test"):
            if not self._authorized():
                return
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 20000)
                data = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/settings":
                    st = save_settings(data)
                    MONITOR.poll()
                    return self._json(public_settings(st))
                ok, err = telegram(f"👋 <b>{TITLE}</b>: Telegram-Test erfolgreich – Alarme kommen hier an.",
                                   load_settings(), force=True)
                return self._json({"ok": ok, "error": err}, 200 if ok else 502)
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": str(e)}, 400)
        handlers = {"/api/custom": upsert_custom, "/api/override": save_override, "/api/layout": save_layout}
        if self.path not in handlers:
            return self._json({"error": "not found"}, 404)
        if not self._authorized():
            return
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 20000)
            data = json.loads(self.rfile.read(n) or b"{}")
            handlers[self.path](data)
            if self.path == "/api/layout":  # ohne kompletten Neuabruf, damit Drag & Drop flott bleibt
                CACHE.apply_assign(data.get("assign") or {})
                return self._json(CACHE.snapshot())
        except (ValueError, json.JSONDecodeError) as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:
            return self._json({"error": f"Speichern fehlgeschlagen: {e}"}, 500)
        CACHE.refresh()
        self._json(CACHE.snapshot())

    def do_DELETE(self):
        m = re.fullmatch(r"/api/custom/([0-9a-f]+)", self.path)
        if not m:
            return self._json({"error": "not found"}, 404)
        if not self._authorized():
            return
        delete_custom(m.group(1))
        CACHE.refresh()
        self._json(CACHE.snapshot())


if __name__ == "__main__":
    threading.Thread(target=CACHE.loop, daemon=True).start()
    threading.Thread(target=MONITOR.loop, daemon=True).start()
    log.info("proxylink läuft auf Port %d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
