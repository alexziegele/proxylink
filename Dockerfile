FROM python:3.12-alpine
WORKDIR /app
COPY app/ /app/
RUN adduser -D -u 1000 app && mkdir -p /config /data && chown app /config /data
USER app
ENV PORT=8080
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:8080/health || exit 1
CMD ["python", "-u", "server.py"]
