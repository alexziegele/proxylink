FROM python:3.12-alpine
RUN apk add --no-cache su-exec \
 && adduser -D -u 1000 app \
 && mkdir -p /config /data
WORKDIR /app
COPY app/ /app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENV PORT=8080 DATA_DIR=/data PUID=1000 PGID=1000
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:8080/health || exit 1
# Startet als root, korrigiert Rechte von /data und läuft dann als unprivilegierter Benutzer
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-u", "server.py"]
