"""Datenquellen: Proxmox VE API und Docker Engine API (nur lesend, nur stdlib)."""
import http.client
import json
import logging
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("proxylink")


def _ctx(insecure):
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Interfaces, deren IPs nicht zum Gast "gehören" (Loopback, Docker-Bridges, ...)
_SKIP_IF = ("lo", "docker", "br-", "veth", "virbr", "cni", "flannel", "tailscale", "zt")


def _skip_if(name):
    return bool(name) and (name == "lo" or name.startswith(_SKIP_IF[1:]))


# --------------------------------------------------------------------------- Proxmox
class Proxmox:
    """Liest LXC + VMs inkl. IPs. Benötigt API-Token, z. B. 'home@pve!dash=UUID'.

    Rechte: PVEAuditor auf / reicht für Liste + LXC-IPs. VM-IPs kommen über den
    QEMU-Guest-Agent (zusätzlich VM.GuestAgent.Audit bzw. bei PVE 8 VM.Monitor).
    """

    def __init__(self, url, token, insecure=False, name="pve"):
        self.name = name
        self.url = url.rstrip("/")
        self.token = token
        self.ctx = _ctx(insecure)

    def _get(self, path):
        req = urllib.request.Request(self.url + "/api2/json" + path,
                                     headers={"Authorization": "PVEAPIToken=" + self.token})
        with urllib.request.urlopen(req, context=self.ctx, timeout=8) as r:
            return json.loads(r.read())["data"]

    @staticmethod
    def _why(e, agent=False):
        """Verständlicher Grund für einen API-Fehler."""
        if isinstance(e, urllib.error.HTTPError):
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            low = body.lower()
            if e.code == 401:
                return "Token ungültig oder abgelaufen (401)"
            if e.code == 403:
                return ("keine Rechte (Token braucht VM.GuestAgent.Audit bzw. VM.Monitor)" if agent
                        else "keine Rechte (Token braucht die Rolle PVEAuditor auf /)")
            if "not running" in low or "no qemu guest agent" in low or "not configured" in low:
                return "QEMU Guest Agent läuft nicht in der VM"
            return f"API-Fehler {e.code}"
        if isinstance(e, urllib.error.URLError):
            return f"nicht erreichbar ({e.reason})"
        return str(e)[:160]

    @staticmethod
    def _cfg_ips(cfg, prefixes):
        """Statische IPs aus LXC-netX (ip=) bzw. VM-cloud-init ipconfigX (ip=)."""
        ips = set()
        for k, v in (cfg or {}).items():
            if any(k.startswith(p) for p in prefixes) and isinstance(v, str) and "ip=" in v:
                ip = v.split("ip=")[1].split(",")[0].split("/")[0]
                if ip not in ("dhcp", "manual", "auto"):
                    ips.add(ip)
        return ips

    def _ips_lxc(self, g):
        ips, err = set(), None
        if g.get("status") == "running":
            try:
                for i in self._get(f"/nodes/{g['node']}/lxc/{g['vmid']}/interfaces") or []:
                    if _skip_if(i.get("name")):
                        continue
                    for key in ("inet", "ip-addresses"):
                        v = i.get(key)
                        if isinstance(v, str):
                            ips.add(v.split("/")[0])
                        elif isinstance(v, list):
                            ips.update(a.get("ip-address", "") for a in v)
                if ips:
                    return ips, "live", None
            except Exception as e:
                err = self._why(e)
        try:
            ips = self._cfg_ips(self._get(f"/nodes/{g['node']}/lxc/{g['vmid']}/config"), ("net",))
            if ips:
                return ips, "config", None
        except Exception as e:
            err = err or self._why(e)
        return set(), None, err or "DHCP ohne laufenden Container – keine IP bekannt"

    def _ips_qemu(self, g):
        cfg, err = {}, None
        try:
            cfg = self._get(f"/nodes/{g['node']}/qemu/{g['vmid']}/config") or {}
        except Exception as e:
            err = self._why(e)
        agent_on = str(cfg.get("agent", "")).split(",")[0] in ("1", "enabled=1")
        if g.get("status") == "running" and agent_on:
            try:
                res = self._get(f"/nodes/{g['node']}/qemu/{g['vmid']}/agent/network-get-interfaces")
                ips = set()
                for i in res.get("result", []):
                    if _skip_if(i.get("name")):
                        continue
                    for a in i.get("ip-addresses", []):
                        ips.add(a.get("ip-address", ""))
                if ips:
                    return ips, "agent", None
            except Exception as e:
                err = self._why(e, agent=True)
        elif g.get("status") == "running" and not err:
            err = "Guest Agent in den VM-Optionen nicht aktiviert"
        ips = self._cfg_ips(cfg, ("ipconfig",))
        if ips:
            return ips, "cloud-init", None
        if g.get("status") != "running":
            err = err or "VM gestoppt"
        return set(), None, err

    def node_hosts(self):
        """Die Proxmox-Nodes selbst als Ziel (Web-UI :8006, PBS auf dem Host …), mit allen Host-IPs."""
        out = []
        try:
            status = {n["name"]: n for n in self._get("/cluster/status") if n.get("type") == "node"}
        except Exception:
            status = {}
        try:
            names = [r["node"] for r in self._get("/nodes")]
        except Exception:
            names = list(status)
        for name in names:
            ips = set()
            if status.get(name, {}).get("ip"):
                ips.add(status[name]["ip"])
            try:
                for i in self._get(f"/nodes/{name}/network") or []:
                    for k in ("address", "address6"):
                        if i.get(k):
                            ips.add(str(i[k]).split("/")[0])
                    if i.get("cidr"):
                        ips.add(str(i["cidr"]).split("/")[0])
            except Exception:
                pass
            online = status.get(name, {}).get("online", 1)
            out.append({"cluster": self.name, "kind": "node", "vmid": "", "name": name, "node": name,
                        "status": "running" if online else "stopped", "ips": sorted(ips),
                        "ip_source": "node", "ip_error": None if ips else "keine Host-IP lesbar (Sys.Audit?)"})
        return out

    def status(self, wanted=()):
        """Nodes (CPU/RAM/Root-Disk) + ausgewählte Storages (per ID oder Pfad, z. B. /mnt/share)."""
        res = self._get("/cluster/resources")
        nodes = [{
            "cluster": self.name, "name": r["node"], "status": r.get("status", "unknown"),
            "cpu": round(r.get("cpu", 0) * 100, 1), "maxcpu": r.get("maxcpu", 0),
            "mem": r.get("mem", 0), "maxmem": r.get("maxmem", 0),
            "disk": r.get("disk", 0), "maxdisk": r.get("maxdisk", 0),
            "uptime": r.get("uptime", 0),
        } for r in res if r.get("type") == "node"]
        nodes.sort(key=lambda n: n["name"])

        storages = []
        if wanted:
            try:
                paths = {s["storage"]: s.get("path", "") for s in self._get("/storage")}
            except Exception:
                paths = {}
            seen = set()
            for r in res:
                if r.get("type") != "storage":
                    continue
                sid = r["storage"]
                if sid not in wanted and paths.get(sid) not in wanted:
                    continue
                if r.get("shared") and sid in seen:  # Shared Storage nur einmal
                    continue
                seen.add(sid)
                storages.append({
                    "cluster": self.name, "id": sid, "path": paths.get(sid, ""), "node": r.get("node"),
                    "shared": bool(r.get("shared")), "status": r.get("status", ""),
                    "disk": r.get("disk", 0), "maxdisk": r.get("maxdisk", 0),
                })
        return nodes, storages

    def guests(self):
        items = [g for g in self._get("/cluster/resources?type=vm") if g.get("template", 0) != 1]

        def enrich(g):
            ips, src, err = self._ips_lxc(g) if g["type"] == "lxc" else self._ips_qemu(g)
            ips = {ip for ip in ips if ip and ip not in ("127.0.0.1", "::1") and not ip.lower().startswith("fe80")}
            return {
                "ip_source": src if ips else None, "ip_error": None if ips else err,
                "cluster": self.name,
                "kind": g["type"],  # lxc | qemu
                "vmid": g["vmid"], "name": g.get("name", str(g["vmid"])),
                "node": g.get("node"), "status": g.get("status"),
                "ips": sorted(ips),
            }

        with ThreadPoolExecutor(8) as ex:
            guests = list(ex.map(enrich, items))
        return guests + self.node_hosts()


# --------------------------------------------------------------------------- Docker
class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=8)
        self.path_ = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(8)
        s.connect(self.path_)
        self.sock = s


class Docker:
    """Ein Docker-Host. url = unix:///var/run/docker.sock | tcp://IP:2375 | http(s)://...

    host_ip = IP, unter der veröffentlichte Ports erreichbar sind (bei tcp:// automatisch).
    Empfehlung für entfernte Hosts: tecnativa/docker-socket-proxy mit CONTAINERS=1 (read-only).
    """

    def __init__(self, name, url, host_ip=None):
        self.name, self.url = name, url
        p = urllib.parse.urlparse(url.replace("tcp://", "http://"))
        self.host_ip = host_ip or (p.hostname if p.scheme in ("http", "https") else None)

    def _get(self, path):
        if self.url.startswith("unix://"):
            c = _UnixConn(self.url[len("unix://"):])
        else:
            p = urllib.parse.urlparse(self.url.replace("tcp://", "http://"))
            cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
            c = cls(p.hostname, p.port or (443 if p.scheme == "https" else 2375), timeout=8)
        c.request("GET", path)
        r = c.getresponse()
        data = r.read()
        c.close()
        if r.status != 200:
            raise RuntimeError(f"Docker {self.name}: HTTP {r.status}")
        return json.loads(data)

    def containers(self):
        out = []
        for c in self._get("/containers/json?all=1"):
            nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
            out.append({
                "host": self.name, "host_ip": self.host_ip,
                "id": c["Id"][:12],
                "name": (c.get("Names") or ["/?"])[0].lstrip("/"),
                "image": c.get("Image", ""),
                "state": c.get("State", ""), "status": c.get("Status", ""),
                "ips": sorted({n.get("IPAddress") for n in nets.values() if n.get("IPAddress")}),
                "ports": sorted({p["PublicPort"] for p in c.get("Ports", []) if p.get("PublicPort")}),
                "private_ports": sorted({p["PrivatePort"] for p in c.get("Ports", []) if p.get("PrivatePort")}),
                "compose": (c.get("Labels") or {}).get("com.docker.compose.project", ""),
            })
        return out


def parse_proxmox(env):
    """PVE_URL/PVE_TOKEN/PVE_NAME und weitere Cluster als PVE_URL_2/PVE_TOKEN_2/PVE_NAME_2 … _9."""
    out = []
    for suffix in [""] + [f"_{i}" for i in range(2, 10)]:
        url, token = env.get("PVE_URL" + suffix, ""), env.get("PVE_TOKEN" + suffix, "")
        if not (url and token):
            continue
        insecure = env.get("PVE_INSECURE" + suffix, env.get("PVE_INSECURE", "true")).lower() in ("1", "true", "yes")
        name = env.get("PVE_NAME" + suffix) or urllib.parse.urlparse(url).hostname or f"pve{suffix}"
        out.append(Proxmox(url, token, insecure, name))
    return out


def parse_docker_hosts(spec):
    """DOCKER_HOSTS='name=url[@host-ip];name2=url2'"""
    hosts = []
    for part in [p.strip() for p in (spec or "").split(";") if p.strip()]:
        name, _, rest = part.partition("=")
        url, _, ip = rest.partition("@")
        hosts.append(Docker(name.strip(), url.strip(), ip.strip() or None))
    return hosts
