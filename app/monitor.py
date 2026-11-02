"""Proxmox-Status (Nodes, Storages, lokale Pfade) + Telegram-Alarme bei Grenzwerten."""
import json
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from store import write_json

log = logging.getLogger("proxylink")


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


STATS_SECONDS = int(os.getenv("STATS_SECONDS", "15"))
PVE_STORAGES = [s.strip() for s in os.getenv("PVE_STORAGES", "/mnt/share").split(",") if s.strip()]
LOCAL_DISKS = [s.strip() for s in os.getenv("LOCAL_DISKS", "").split(",") if s.strip()]  # name=/pfad im Container

DATA_DIR = os.getenv("DATA_DIR", "/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# Standardwerte aus der Umgebung; im Panel geänderte Werte (settings.json) haben Vorrang
DEFAULTS = {
    "telegram_enabled": True,
    "telegram_token": os.getenv("TELEGRAM_TOKEN", ""),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    "cpu": _env_float("ALERT_CPU", 90),          # % (0 = aus)
    "ram": _env_float("ALERT_RAM", 90),          # % (0 = aus)
    "disk": _env_float("ALERT_DISK", 0),         # % (0 = aus)
    "minutes": _env_float("ALERT_MINUTES", 2),   # so lange muss der Wert drüber sein
    "hysteresis": _env_float("ALERT_HYSTERESIS", 5),
    "repeat": _env_float("ALERT_REPEAT_MINUTES", 0),
    "node_down": os.getenv("ALERT_NODE_DOWN", "true").lower() in ("1", "true", "yes"),
}
NUMERIC = {"cpu": (0, 100), "ram": (0, 100), "disk": (0, 100), "minutes": (0, 1440),
           "hysteresis": (0, 50), "repeat": (0, 10080)}
_settings_lock = threading.Lock()


def load_settings():
    st = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            st.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("settings.json nicht lesbar: %s", e)
    return st


def save_settings(data):
    with _settings_lock:
        st = load_settings()
        for k, (lo, hi) in NUMERIC.items():
            if k in data and str(data[k]).strip() != "":
                v = float(data[k])
                if not lo <= v <= hi:
                    raise ValueError(f"{k} muss zwischen {lo} und {hi} liegen")
                st[k] = v
        for k in ("telegram_enabled", "node_down"):
            if k in data:
                st[k] = bool(data[k])
        if "telegram_chat_id" in data:
            st["telegram_chat_id"] = str(data["telegram_chat_id"]).strip()
        # Token nur überschreiben, wenn ein neuer eingegeben wurde; "-" löscht ihn
        tok = str(data.get("telegram_token", "")).strip()
        if tok == "-":
            st["telegram_token"] = ""
        elif tok:
            st["telegram_token"] = tok
        write_json(SETTINGS_FILE, st)
        return st


def public_settings(st=None):
    """Für das Panel – ohne den Token im Klartext."""
    st = dict(st or load_settings())
    tok = st.pop("telegram_token")
    st["token_set"] = bool(tok)
    st["token_hint"] = ("…" + tok[-4:]) if len(tok) > 8 else ""
    return st


TITLE = os.getenv("TITLE", "proxylink")


def pct(a, b):
    return round(a / b * 100, 1) if b else 0.0


def gb(x):
    return f"{x / 1024**3:.1f} GB" if x < 1024**4 else f"{x / 1024**4:.2f} TB"


def telegram_ready(st=None):
    st = st or load_settings()
    return bool(st["telegram_enabled"] and st["telegram_token"] and st["telegram_chat_id"])


def telegram(text, st=None, force=False):
    """Sendet eine Nachricht. Gibt (ok, fehlertext) zurück."""
    st = st or load_settings()
    if not (st["telegram_token"] and st["telegram_chat_id"]):
        return False, "Bot-Token oder Chat-ID fehlt"
    if not (st["telegram_enabled"] or force):
        return False, "Telegram ist deaktiviert"
    data = urllib.parse.urlencode({"chat_id": st["telegram_chat_id"], "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{st['telegram_token']}/sendMessage",
                               data, timeout=10).read()
        return True, ""
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("description", str(e))
        except Exception:
            msg = str(e)
        log.warning("Telegram fehlgeschlagen: %s", msg)
        return False, msg
    except Exception as e:
        log.warning("Telegram fehlgeschlagen: %s", e)
        return False, str(e)


class Monitor:
    def __init__(self, pve):
        self.pves = pve or []
        self.lock = threading.Lock()
        self.data = {"nodes": [], "storages": [], "error": None, "updated": 0}
        self.alerts = {}  # key -> {"since": ts, "active": bool, "sent": ts}

    # ------------------------------------------------------------------ Abruf
    def poll(self):
        nodes, storages, err = [], [], None
        errs = []
        for p in self.pves:
            try:
                n, s = p.status(PVE_STORAGES)
                nodes += n
                storages += s
            except Exception as e:
                errs.append(f"{p.name}: {e}")
                log.warning("Proxmox-Status %s nicht abrufbar: %s", p.name, e)
        err = "; ".join(errs) or None
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
            st = load_settings()
            self.data = {"nodes": nodes, "storages": storages, "error": err, "updated": time.time(),
                         "thresholds": {"cpu": st["cpu"], "ram": st["ram"], "disk": st["disk"]},
                         "telegram": telegram_ready(st)}
        self.check(nodes, storages, st)
        with self.lock:
            self.data["active"] = sorted(k for k, a in self.alerts.items() if a["active"])

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def loop(self):
        while True:
            self.poll()
            time.sleep(STATS_SECONDS)

    # ------------------------------------------------------------------ Alarme
    def _eval(self, key, value, limit, msg_on, msg_off, hyst=None):
        """value >= limit für st["minutes"] → Alarm; < limit - Hysterese → Entwarnung."""
        st = self.st
        hyst = st["hysteresis"] if hyst is None else hyst
        ready = telegram_ready(st)
        now = time.time()
        a = self.alerts.setdefault(key, {"since": None, "active": False, "sent": 0})
        if value >= limit:
            a["since"] = a["since"] or now
            due = now - a["since"] >= st["minutes"] * 60
            repeat = a["active"] and st["repeat"] and now - a["sent"] >= st["repeat"] * 60
            if (due and not a["active"]) or repeat:
                log.warning("ALARM %s: %s", key, value)
                if not ready or telegram(msg_on, st)[0]:
                    a["active"], a["sent"] = True, now
        elif value < limit - hyst:
            if a["active"] and ready:
                telegram(msg_off, st)
                log.info("Entwarnung %s", key)
            a.update(since=None, active=False)
        else:
            a["since"] = None if not a["active"] else a["since"]

    def check(self, nodes, storages, st=None):
        self.st = st = st or load_settings()
        ALERT_CPU, ALERT_RAM, ALERT_DISK, ALERT_MINUTES = st["cpu"], st["ram"], st["disk"], st["minutes"]
        # abgeschaltete Prüfungen vergessen
        for key in list(self.alerts):
            kind = key.rsplit(":", 1)[-1]
            if (kind == "cpu" and not ALERT_CPU) or (kind == "ram" and not ALERT_RAM) or \
               (kind == "down" and not st["node_down"]) or \
               ((kind == "disk" or key.startswith("storage:")) and not ALERT_DISK):
                self.alerts.pop(key)
        multi = len({n.get("cluster") for n in nodes}) > 1
        for n in nodes:
            # bei mehreren Clustern eindeutige Schlüssel/Namen
            name = f"{n['name']} ({n['cluster']})" if multi else n["name"]
            if st["node_down"]:
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
                self._eval(f"storage:{s.get('cluster', '')}/{s['id']}", s["pct"], ALERT_DISK,
                           f"💾 <b>{TITLE}</b>: Speicher <b>{label}</b> bei <b>{s['pct']:.0f} %</b> "
                           f"({gb(s['disk'])} von {gb(s['maxdisk'])})",
                           f"✅ <b>{TITLE}</b>: Speicher <b>{label}</b> wieder bei {s['pct']:.0f} %")
