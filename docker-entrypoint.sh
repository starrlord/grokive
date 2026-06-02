#!/usr/bin/env bash
set -e

# Unraid-style ownership: run as PUID:PGID (defaults nobody:users = 99:100)
PUID="${PUID:-99}"
PGID="${PGID:-100}"

if [ "$(id -u)" = "0" ]; then
    groupmod -o -g "$PGID" users 2>/dev/null || groupadd -o -g "$PGID" appgroup 2>/dev/null || true
    GROUP_NAME="$(getent group "$PGID" | cut -d: -f1)"
    GROUP_NAME="${GROUP_NAME:-users}"

    if ! id appuser >/dev/null 2>&1; then
        useradd -o -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin appuser 2>/dev/null || true
    else
        usermod -o -u "$PUID" -g "$PGID" appuser 2>/dev/null || true
    fi

    mkdir -p "${GROK_DATA_DIR:-/data}"
    chown -R "$PUID:$PGID" "${GROK_DATA_DIR:-/data}" || true

    echo "Starting as ${PUID}:${PGID} (data: ${GROK_DATA_DIR:-/data})"
    exec gosu "$PUID:$PGID" "$@"
fi

# Already non-root (e.g. compose user: directive) — just run.
exec "$@"
