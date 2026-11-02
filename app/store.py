"""Sicheres Lesen/Schreiben der Daten-Dateien in DATA_DIR."""
import json
import logging
import os

log = logging.getLogger("proxylink")
DATA_DIR = os.getenv("DATA_DIR", "/data")


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.warning("%s nicht lesbar: %s", path, e)
        return default


def write_json(path, data):
    """Atomar schreiben: erst .tmp + fsync, dann umbenennen – bricht nie eine halbe Datei ab."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:  # Verzeichniseintrag ebenfalls sichern
        fd = os.open(os.path.dirname(path), os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass


def check_writable():
    """Gibt None zurück, wenn DATA_DIR beschreibbar ist, sonst eine Fehlermeldung."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        test = os.path.join(DATA_DIR, ".write-test")
        with open(test, "w") as f:
            f.write("ok")
            f.flush()
            os.fsync(f.fileno())
        os.remove(test)
        return None
    except OSError as e:
        return f"{DATA_DIR} ist nicht beschreibbar ({e.strerror or e}). Änderungen gehen beim Neustart verloren."
