"""Datenquellen: Proxmox VE API und Docker Engine API (nur lesend, nur stdlib)."""
import http.client
import json
import logging
import socket
import ssl
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

    def __init__(self, url, token, insecure=False):
        self.url = url.rstrip("/")
        self.token = token
        self.ctx = _ctx(insecure)

    def _get(self, path):
        req = urllib.request.Request(self.url + "/api2/json" + path,
                                     headers={"Authorization": "PVEAPIToken=" + self.token})
        with urllib.request.urlopen(req, context=self.ctx, timeout=8) as r:
            return json.loads(r.read())["data"]

    def _ips_lxc(self, g):
        ips = set()
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
        except Exception:
            pass
        if not ips:  # gestoppt oder alte PVE-Version: statische IP aus der Config
            try:
                cfg = self._get(f"/nodes/{g['node']}/lxc/{g['vmid']}/config")
                for k, v in cfg.items():
                    if k.startswith("net") and "ip=" in v:
                        ip = v.split("ip=")[1].split(",")[0].split("/")[0]
                        if ip not in ("dhcp", "manual"):
                            ips.add(ip)
            except Exception:
                pass
        return ips

    def _ips_qemu(self, g):
        ips = set()
        if g.get("status") != "running":
            return ips
        try:
            res = self._get(f"/nodes/{g['node']}/qemu/{g['vmid']}/agent/network-get-interfaces")
            for i in res.get("result", []):
                if _skip_if(i.get("name")):
                    continue
                for a in i.get("ip-addresses", []):
                    ips.add(a.get("ip-address", ""))
        except Exception:
            pass  # kein Guest-Agent / keine Rechte
        return ips

    def status(self, wanted=()):
        """Nodes (CPU/RAM/Root-Disk) + ausgewählte Storages (per ID oder Pfad, z. B. /mnt/share)."""
        res = self._get("/cluster/resources")
        nodes = [{
            "name": r["node"], "status": r.get("status", "unknown"),
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
                    "id": sid, "path": paths.get(sid, ""), "node": r.get("node"),
                    "shared": bool(r.get("shared")), "status": r.get("status", ""),
                    "disk": r.get("disk", 0), "maxdisk": r.get("maxdisk", 0),
                })
        return nodes, storages

    def guests(self):
        items = [g for g in self._get("/cluster/resources?type=vm") if g.get("template", 0) != 1]

        def enrich(g):
            ips = self._ips_lxc(g) if g["type"] == "lxc" else self._ips_qemu(g)
            ips = {ip for ip in ips if ip and ip not in ("127.0.0.1", "::1") and not ip.lower().startswith("fe80")}
            return {
                "kind": g["type"],  # lxc | qemu
                "vmid": g["vmid"], "name": g.get("name", str(g["vmid"])),
                "node": g.get("node"), "status": g.get("status"),
                "ips": sorted(ips),
            }

        with ThreadPoolExecutor(8) as ex:
            return list(ex.map(enrich, items))


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


def parse_docker_hosts(spec):
    """DOCKER_HOSTS='name=url[@host-ip];name2=url2'"""
    hosts = []
    for part in [p.strip() for p in (spec or "").split(";") if p.strip()]:
        name, _, rest = part.partition("=")
        url, _, ip = rest.partition("@")
        hosts.append(Docker(name.strip(), url.strip(), ip.strip() or None))
    return hosts
