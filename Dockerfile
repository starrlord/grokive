# --- Stage 1: build the SvelteKit SPA ---
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
# Drop the lockfile before install so npm fully re-resolves optional deps for THIS
# build platform (Linux/musl). Tailwind 4's Oxide engine (and Rollup/Lightning CSS)
# ship per-platform native binaries; npm won't install the linux-musl ones from a
# lockfile generated on another OS (e.g. Windows) — see npm/cli#4828. The error even
# instructs: "remove both package-lock.json and node_modules". Versions are still
# pinned by semver ranges in package.json.
RUN rm -f package-lock.json && npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- Stage 2: Python app server ---
FROM python:3.12-slim

# ffmpeg = video thumbnails / merge / burn; gosu = drop to PUID/PGID for Unraid-friendly file ownership
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt

COPY *.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Built SPA served at "/" by server.py (SPA_DIR defaults to /app/web/build)
COPY --from=web /web/build ./web/build

ENV GROK_DATA_DIR=/data \
    PORT=8080 \
    PUID=99 \
    PGID=100 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz')" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "server.py"]
