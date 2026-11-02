#!/bin/sh
# Bereitet /data vor und startet proxylink ohne root-Rechte.
set -e

if [ "$(id -u)" = "0" ]; then
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"

  # Benutzer/Gruppe an PUID/PGID anpassen (z. B. damit ./data auf dem Host dir gehört)
  if [ "$(id -u app)" != "$PUID" ] || [ "$(id -g app)" != "$PGID" ]; then
    deluser app 2>/dev/null || true
    delgroup app 2>/dev/null || true
    getent group "$PGID" >/dev/null || addgroup -g "$PGID" app
    adduser -D -H -u "$PUID" -G "$(getent group "$PGID" | cut -d: -f1)" app
  fi

  mkdir -p "${DATA_DIR:-/data}"
  chown -R "$PUID:$PGID" "${DATA_DIR:-/data}"

  # Lesezugriff auf den Docker-Socket: Gruppe des Sockets übernehmen
  if [ -S /var/run/docker.sock ]; then
    SOCK_GID="$(stat -c %g /var/run/docker.sock)"
    SOCK_GROUP="$(getent group "$SOCK_GID" | cut -d: -f1)"
    if [ -z "$SOCK_GROUP" ]; then
      addgroup -g "$SOCK_GID" dockersock
      SOCK_GROUP=dockersock
    fi
    addgroup app "$SOCK_GROUP" 2>/dev/null || true
  fi

  exec su-exec app "$@"
fi

exec "$@"
