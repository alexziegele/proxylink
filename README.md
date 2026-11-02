<p align="center"><img src="app/static/logo.svg" width="96" alt="proxylink"></p>

# proxylink

Kleine Startseite fürs Homelab: holt alle HTTP-Proxy-Regeln aus **Zoraxy** und zeigt sie als Links an.
Jede Route wird bis zum Ziel verfolgt und mit **Proxmox** (LXC + VMs) und **Docker**-Containern abgeglichen.

- **Links**: Kacheln pro Zoraxy-Tag, Status-Punkt (online / teilweise / offline), Ziel z. B. `LXC 105 docker01 → 🐳 jellyfin`
- **Abgleich**: Route → Origin → Proxmox-Gast → Container → Port erreichbar?
  Dazu: Routen ohne bekanntes Ziel, Proxmox-Gäste ohne Route, Container mit veröffentlichten Ports ohne Route
- **Ohne Proxy**: laufende Dienste ohne Zoraxy-Route (Container mit veröffentlichten Ports, offene Ports auf Proxmox-Gästen) erscheinen als gestrichelte Kacheln
- In Zoraxy deaktivierte Proxies werden ausgegraut und mit Aus-Schalter markiert
- Unter jeder Kachel `ip:port` zum Kopieren
- **Mehrere Proxmox-Cluster** (`PVE_URL_2` …), Status gruppiert je Cluster
- **Proxmox-Status** oben: alle Nodes mit CPU, RAM, Root-Disk + ausgewählte Storages (z. B. `/mnt/share`)
- **Telegram-Alarme** (im Panel unter 🔔 einstellbar), wenn CPU/RAM (optional Disk) eine Grenze für eine Weile überschreiten, plus Entwarnung und Node-offline-Meldung
- **Jede Kachel anpassbar** (✎): Name, Icon, Gruppe, Beschreibung, URL, ausblenden. Icons per Name aus [dashboardicons.com](https://dashboardicons.com) oder URL, ohne Angabe wird automatisch nach Name/Subdomain gesucht
- **Anordnen** (✥): Kacheln und Proxmox-Hosts per Drag & Drop in Kategorien verschieben, Kategorien umbenennen,
  sortieren, anlegen und leere entfernen. Auf dem Handy mit ↑/↓ und Kategorie-Auswahl. Gespeichert in `/data/layout.json`
- **Mobil**: kompakter Header, Suche in eigener Zeile, Statusleiste in drei Spalten, Dialoge in voller Breite
- **Ausblenden**: jede Kachel (auch Proxmox-Hosts und eigene Einträge) über das durchgestrichene Auge; unten unter
  „Ausgeblendet“ wieder einblenden
- **Eigene Einträge** über „+ Eintrag“ (bearbeiten per ✎ auf der Kachel), gespeichert in `./data`
- Suche mit `/`, `Enter` öffnet den ersten Treffer
- Nur Python-Standardbibliothek, Image ~50 MB, alle Quellen nur lesend

## Start

```bash
cp .env.example .env              # Werte eintragen
cp config/overrides.example.json config/overrides.json   # optional
docker compose up -d --build
```

Dann `http://<host>:8080` öffnen.

## Daten & Sicherung

Alles, was du im Panel einstellst (Anordnung, Anpassungen, Ausgeblendetes, eigene Einträge, Alarme), liegt im Ordner
**`./data`** neben der `docker-compose.yml`. Der Container setzt die Rechte beim Start selbst (`PUID`/`PGID`,
Standard 1000). Den Ordner einfach mitsichern, oder unten im Panel **Sicherung → Export/Import**
(der Telegram-Token wird nicht exportiert).

Ist `/data` nicht beschreibbar, zeigt das Panel oben eine rote Warnung, und jede fehlgeschlagene Änderung wird gemeldet.

**Umstieg von älteren Versionen** (Daten lagen im Docker-Volume `proxylink_proxylink-data`):

```bash
mkdir -p data && docker run --rm -v proxylink_proxylink-data:/from -v "$PWD/data":/to alpine cp -a /from/. /to/
```

## Konfiguration (`.env`)

| Variable | Bedeutung |
|---|---|
| `ZORAXY_URL` | Admin-UI von Zoraxy, z. B. `http://192.168.10.5:8000` |
| `ZORAXY_USER` / `ZORAXY_PASS` | Login der Zoraxy-Web-UI (leer lassen bei `-noauth`) |
| `ZORAXY_INSECURE` | `true` bei https mit selbstsigniertem Zertifikat |
| `PVE_URL` / `PVE_TOKEN` / `PVE_NAME` | Proxmox-API, Token im Format `user@realm!tokenid=secret`, Name zur Anzeige |
| `PVE_URL_2` … `_9` | weitere eigenständige Cluster, jeweils mit `PVE_TOKEN_n`, `PVE_NAME_n`, optional `PVE_INSECURE_n` |
| `PVE_INSECURE` | Zertifikat nicht prüfen (Standard `true`) |
| `DOCKER_HOSTS` | `name=url[@host-ip]`, mehrere mit `;` |
| `LINK_SCHEME` | `https` oder `http` für die erzeugten Links |
| `CHECK_ORIGINS` | TCP-Check auf jeden Origin (`true`) |
| `SHOW_DISABLED` | deaktivierte Regeln mit anzeigen (Standard `true`) |
| `SHOW_UNPROXIED` | Dienste ohne Proxy-Route anzeigen (Standard `true`) |
| `PROBE_PORTS` | Ports, die auf Proxmox-Gästen ohne Route geprüft werden |
| `PVE_STORAGES` | Storages für die Statusleiste (ID oder Pfad), Standard `/mnt/share` |
| `LOCAL_DISKS` | zusätzlich in den Container gemountete Pfade `name=/pfad` |
| `TELEGRAM_TOKEN`, `ALERT_*` | nur Startwerte, eingestellt wird im Panel (🔔 Alarme) |
| `EDIT_TOKEN` | optional: Token, ohne den keine eigenen Einträge angelegt/gelöscht werden können |
| `EXCLUDE` | Domains mit diesen Teilstrings ausblenden, kommagetrennt |
| `REFRESH_SECONDS` | Abrufintervall (Standard 60) |

### Proxmox-Token

Datacenter → Permissions → Users: z. B. `proxylink@pve` anlegen, dann API-Token `dash` (Privilege Separation aus)
und die Rolle **PVEAuditor** auf `/` vergeben.
LXC-IPs kommen über `/lxc/<id>/interfaces` bzw. die statische `ip=` aus der Config.
VM-IPs gibt es nur mit installiertem **QEMU Guest Agent** und dem Recht `VM.GuestAgent.Audit` (PVE 9) bzw. `VM.Monitor` (PVE 8).

### Docker

- **Lokal:** Socket ist in der Compose-Datei read-only gemountet. `DOCKER_GID` = Gruppe von `/var/run/docker.sock` (`stat -c %g /var/run/docker.sock`).
  Gib `@host-ip` an, damit veröffentlichte Ports der Host-IP zugeordnet werden können.
- **Entfernt:** auf dem anderen Host einen read-only Socket-Proxy starten und per `tcp://` eintragen:

```yaml
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy
    environment: { CONTAINERS: 1 }
    volumes: [ "/var/run/docker.sock:/var/run/docker.sock:ro" ]
    ports: [ "2375:2375" ]   # nur im internen VLAN freigeben!
```

## So wird eine Route zugeordnet

Für jeden Origin (`host:port`) einer Zoraxy-Regel:

1. Hostname = Containername → Container (Zoraxy im selben Docker-Netz)
2. IP = `host-ip` eines Docker-Hosts **und** Port ist dort veröffentlicht → Container
3. IP = Container-IP (macvlan/ipvlan) → Container
4. IP gehört einem Proxmox-Gast → Gast; läuft der Container auf einem Docker-Host, der selbst ein Gast ist, wird die Kette `LXC → Container` angezeigt
5. Fallback: erster Teil des Hostnamens = Gastname

IPs von `lo`, `docker*`, `br-*`, `veth*` usw. werden bei Gästen ignoriert.

## Overrides

`config/overrides.json` – pro Domain optional Name, Gruppe, Icon, Beschreibung, eigene URL oder ausblenden:

```json
{
  "pve.example.de": { "name": "Proxmox", "group": "Infrastruktur", "icon": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/proxmox.svg" },
  "test.example.de": { "hide": true }
}
```

Ohne Override ist die Gruppe der erste Zoraxy-Tag (sonst „Allgemein“).

## Speicher /mnt/share

Die Statusleiste liest Storages über die Proxmox-API. `/mnt/share` erscheint, wenn es unter **Datacenter → Storage** als Storage
(z. B. Typ *Directory*) mit diesem Pfad eingetragen ist; `PVE_STORAGES` akzeptiert Pfad oder Storage-ID.
Ist es dort nicht eingetragen, kannst du den Pfad in den proxylink-Container mounten (`- /mnt/share:/mnt/share:ro`)
und `LOCAL_DISKS=share=/mnt/share` setzen.

## Telegram-Alarme

Alles im Panel unter **🔔 Alarme**: Bot-Token, Chat-ID, Grenzen für CPU/RAM/Disk, Dauer, Entwarnung, Erinnerung,
Node-offline-Meldung. **Test senden** speichert und schickt eine Testnachricht. Gespeichert wird in `/data/settings.json`.

1. Bei [@BotFather](https://t.me/BotFather) einen Bot anlegen → Token
2. Dem Bot einmal schreiben, Chat-ID z. B. über [@userinfobot](https://t.me/userinfobot)
3. Beides im Panel eintragen → „Test senden“

Ein Alarm kommt einmal, wenn der Wert die eingestellte Dauer über der Grenze liegt, und eine Entwarnung, sobald er
um die Entwarnungs-Spanne darunter fällt. Kurze Spitzen lösen nichts aus. Die Zahl am 🔔-Knopf zeigt aktive Alarme.
Mit `EDIT_TOKEN` ist auch dieser Dialog geschützt.

### Mehrere Proxmox-Cluster

Für jeden eigenständigen Cluster einen eigenen Token anlegen (gleiche Befehle) und als `PVE_URL_2`, `PVE_TOKEN_2`,
`PVE_NAME_2` eintragen. Gäste aller Cluster werden beim Routen-Abgleich berücksichtigt, die Statusleiste gruppiert je Cluster.

## Dienste ohne Proxy

- **Docker:** laufende Container mit veröffentlichten Ports, deren `host-ip:port` von keiner Route genutzt wird
- **Proxmox:** laufende Gäste ohne Route (und die kein Docker-Host sind) werden auf `PROBE_PORTS` geprüft; offene Ports → Kachel
- Ausblenden über `overrides.json` mit dem Container-/Gastnamen oder `ip:port` als Schlüssel: `{ "uptime-kuma": { "hide": true } }`

Hinweis: Kopieren in die Zwischenablage funktioniert auch über `http://`, im Browser ist dafür ein Fallback eingebaut.

## API

- `GET /api/links` – zwischengespeicherter Stand (Links, Gäste, Container)
- `GET /api/refresh` – sofort neu abrufen
- `POST /api/custom` – eigenen Eintrag anlegen/ändern (JSON: `name`, `url`, `group`, `description`, `origin`, `icon`, bei Änderung `id`)
- `DELETE /api/custom/<id>`
- `POST /api/layout` – Kategorien/Reihenfolge (JSON: `groups`, `order`, `assign`, `status`)
- `POST /api/override` – Anpassung einer Kachel (JSON: `key` + `name`, `icon`, `group`, `description`, `url`, `hide`; nur `key` = zurücksetzen)
- `GET /api/status` – Proxmox-Nodes und Storages
- `GET /api/settings`, `POST /api/settings` – Alarm-Einstellungen
- `POST /api/telegram-test`
- `GET /health`
