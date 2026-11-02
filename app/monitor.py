"""Proxmox-Status (Nodes, Storages, lokale Pfade) + Telegram-Alarme bei Grenzwerten."""
import json
import logging
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("proxylink")


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


STATS_SECONDS = int(os.getenv("STATS_SECONDS", "15"))
PVE_STORAGES = [s.strip() for s in os.getenv("PVE_STORAGES", "/mnt/share").split(",") if s.strip()]
LOCAL_DISKS = [s.strip() for s in os.getenv("LOCAL_DISKS", "").split(",") if s.strip()]  # name=/pfad im Container

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
ALERT_CPU = _env_float("ALERT_CPU", 90)      # %
ALERT_RAM = _env_float("ALERT_RAM", 90)      # %
ALERT_DISK = _env_float("ALERT_DISK", 0)     # % (0 = aus)
ALERT_MINUTES = _env_float("ALERT_MINUTES", 2)       # so lange muss der Wert drüber sein
ALERT_REPEAT = _env_float("ALERT_REPEAT_MINUTES", 0)  # Erinnerung, solange es anhält (0 = nie)
HYSTERESIS = _env_float("ALERT_HYSTERESIS", 5)       # Entwarnung erst X % unter der Grenze
ALERT_NODE_DOWN = os.getenv("ALERT_NODE_DOWN", "true").lower() in ("1", "true", "yes")
TITLE = os.getenv("TITLE", "proxylink")


def pct(a, b):
    return round(a / b * 100, 1) if b else 0.0


def gb(x):
    return f"{x / 1024**3:.1f} GB" if x < 1024**4 else f"{x / 1024**4:.2f} TB"


def telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return False
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=10).read()
        return True
    except Exception as e:
        log.warning("Telegram fehlgeschlagen: %s", e)
        return False


class Monitor:
    def __init__(self, pve):
        self.pve = pve
        self.lock = threading.Lock()
        self.data = {"nodes": [], "storages": [], "error": None, "updated": 0}
        self.alerts = {}  # key -> {"since": ts, "active": bool, "sent": ts}

    # ------------------------------------------------------------------ Abruf
    def poll(self):
        nodes, storages, err = [], [], None
        if self.pve:
            try:
                nodes, storages = self.pve.status(PVE_STORAGES)
            except Exception as e:
                err = str(e)
                log.warning("Proxmox-Status nicht abrufbar: %s", e)
        for spec in LOCAL_DISKS:  # direkt in den Container gemountete Pfade
            name, _, path = spec.partition("=")
            path = path or name
            try:
                u = shutil.disk_usage(path)
                storages.append({"id": name, "path": path, "node": "lokal", "shared": False,
                                 "status": "available", "disk": u.used, "maxdisk": u.total})
            except OSError as e:
                log.warning("Pfad %s nicht lesbar: %s", path, e)
        for n in nodes:
            n["mem_pct"], n["disk_pct"] = pct(n["mem"], n["maxmem"]), pct(n["disk"], n["maxdisk"])
        for s in storages:
            s["pct"] = pct(s["disk"], s["maxdisk"])
        with self.lock:
            self.data = {"nodes": nodes, "storages": storages, "error": err, "updated": time.time(),
                         "thresholds": {"cpu": ALERT_CPU, "ram": ALERT_RAM, "disk": ALERT_DISK},
                         "telegram": bool(TG_TOKEN and TG_CHAT)}
        self.check(nodes, storages)

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def loop(self):
        while True:
            self.poll()
            time.sleep(STATS_SECONDS)

    # ------------------------------------------------------------------ Alarme
    def _eval(self, key, value, limit, msg_on, msg_off, hyst=None):
        """value >= limit für ALERT_MINUTES → Alarm; < limit - HYSTERESIS → Entwarnung."""
        hyst = HYSTERESIS if hyst is None else hyst
        now = time.time()
        a = self.alerts.setdefault(key, {"since": None, "active": False, "sent": 0})
        if value >= limit:
            a["since"] = a["since"] or now
            due = now - a["since"] >= ALERT_MINUTES * 60
            repeat = a["active"] and ALERT_REPEAT and now - a["sent"] >= ALERT_REPEAT * 60
            if (due and not a["active"]) or repeat:
                log.warning("ALARM %s: %s", key, value)
                if telegram(msg_on) or not (TG_TOKEN and TG_CHAT):
                    a["active"], a["sent"] = True, now
        elif value < limit - hyst:
            if a["active"]:
                telegram(msg_off)
                log.info("Entwarnung %s", key)
            a.update(since=None, active=False)
        else:
            a["since"] = None if not a["active"] else a["since"]

    def check(self, nodes, storages):
        for n in nodes:
            name = n["name"]
            if ALERT_NODE_DOWN:
                down = 0 if n["status"] == "online" else 1
                self._eval(f"{name}:down", down, 1,
                           f"🔴 <b>{TITLE}</b>: Node <b>{name}</b> ist {n['status']}",
                           f"🟢 <b>{TITLE}</b>: Node <b>{name}</b> ist wieder online", hyst=0)
            if n["status"] != "online":
                continue
            if ALERT_CPU:
                self._eval(f"{name}:cpu", n["cpu"], ALERT_CPU,
                           f"🔥 <b>{TITLE}</b>: CPU auf <b>{name}</b> bei <b>{n['cpu']:.0f} %</b> "
                           f"(Grenze {ALERT_CPU:.0f} %, seit {ALERT_MINUTES:g} min)",
                           f"✅ <b>{TITLE}</b>: CPU auf <b>{name}</b> wieder bei {n['cpu']:.0f} %")
            if ALERT_RAM:
                self._eval(f"{name}:ram", n["mem_pct"], ALERT_RAM,
                           f"🧠 <b>{TITLE}</b>: RAM auf <b>{name}</b> bei <b>{n['mem_pct']:.0f} %</b> "
                           f"({gb(n['mem'])} von {gb(n['maxmem'])}, Grenze {ALERT_RAM:.0f} %)",
                           f"✅ <b>{TITLE}</b>: RAM auf <b>{name}</b> wieder bei {n['mem_pct']:.0f} %")
            if ALERT_DISK:
                self._eval(f"{name}:disk", n["disk_pct"], ALERT_DISK,
                           f"💾 <b>{TITLE}</b>: Root-Disk auf <b>{name}</b> bei <b>{n['disk_pct']:.0f} %</b>",
                           f"✅ <b>{TITLE}</b>: Root-Disk auf <b>{name}</b> wieder bei {n['disk_pct']:.0f} %")
        if ALERT_DISK:
            for s in storages:
                label = s["path"] or s["id"]
                self._eval(f"storage:{s['id']}", s["pct"], ALERT_DISK,
                           f"💾 <b>{TITLE}</b>: Speicher <b>{label}</b> bei <b>{s['pct']:.0f} %</b> "
                           f"({gb(s['disk'])} von {gb(s['maxdisk'])})",
                           f"✅ <b>{TITLE}</b>: Speicher <b>{label}</b> wieder bei {s['pct']:.0f} %")
