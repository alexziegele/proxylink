# proxylink

Kleine Startseite fürs Homelab: holt alle HTTP-Proxy-Regeln aus **Zoraxy** und zeigt sie als Links an.
Jede Route wird bis zum Ziel verfolgt und mit **Proxmox** (LXC + VMs) und **Docker**-Containern abgeglichen.

- **Links**: Kacheln pro Zoraxy-Tag, Status-Punkt (online / teilweise / offline), Ziel z. B. `LXC 105 docker01 → 🐳 jellyfin`
- **Abgleich**: Route → Origin → Proxmox-Gast → Container → Port erreichbar?
  Dazu: Routen ohne bekanntes Ziel, Proxmox-Gäste ohne Route, Container mit veröffentlichten Ports ohne Route
- Suche mit `/`, `Enter` öffnet den ersten Treffer
- Nur Python-Standardbibliothek, Image ~50 MB, alle Quellen nur lesend

## Start

```bash
cp .env.example .env              # Werte eintragen
cp config/overrides.example.json config/overrides.json   # optional
docker compose up -d --build
```

Dann `http://<host>:8080` öffnen.

## Konfiguration (`.env`)

| Variable | Bedeutung |
|---|---|
| `ZORAXY_URL` | Admin-UI von Zoraxy, z. B. `http://192.168.10.5:8000` |
| `ZORAXY_USER` / `ZORAXY_PASS` | Login der Zoraxy-Web-UI (leer lassen bei `-noauth`) |
| `ZORAXY_INSECURE` | `true` bei https mit selbstsigniertem Zertifikat |
| `PVE_URL` / `PVE_TOKEN` | Proxmox-API, Token im Format `user@realm!tokenid=secret` |
| `PVE_INSECURE` | Zertifikat nicht prüfen (Standard `true`) |
| `DOCKER_HOSTS` | `name=url[@host-ip]`, mehrere mit `;` |
| `LINK_SCHEME` | `https` oder `http` für die erzeugten Links |
| `CHECK_ORIGINS` | TCP-Check auf jeden Origin (`true`) |
| `SHOW_DISABLED` | deaktivierte Regeln mit anzeigen |
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

## API

- `GET /api/links` – zwischengespeicherter Stand (Links, Gäste, Container)
- `GET /api/refresh` – sofort neu abrufen
- `GET /health`
