#!/bin/sh
set -e

# Default PUID and PGID if not provided
PUID=${PUID:-1000}
PGID=${PGID:-1000}
UMASK=${UMASK:-022}

if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    echo "ERROR: Running as root (PUID=0 or PGID=0) is strictly prohibited." >&2
    exit 1
fi

umask "$UMASK"

if [ "$(id -u)" = "0" ]; then
    # Ensure group exists with PGID
    if ! getent group "$PGID" >/dev/null 2>&1; then
        if getent group appgroup >/dev/null 2>&1; then
            groupmod -o -g "$PGID" appgroup 2>/dev/null || true
        else
            groupadd -o -g "$PGID" appgroup 2>/dev/null || true
        fi
    fi

    # Ensure user exists with PUID and PGID
    if id -u appuser >/dev/null 2>&1; then
        CURRENT_UID=$(id -u appuser 2>/dev/null || echo "")
        CURRENT_GID=$(id -g appuser 2>/dev/null || echo "")
        if [ "$CURRENT_UID" != "$PUID" ] || [ "$CURRENT_GID" != "$PGID" ]; then
            usermod -o -u "$PUID" -g "$PGID" appuser 2>/dev/null || true
        fi
    else
        useradd -o -u "$PUID" -g "$PGID" -d /home/appuser -m appuser 2>/dev/null || true
    fi

    # Ensure /config, /data, /music, and /downloads exist and adjust ownership
    mkdir -p /config /data /data/media/music /data/downloads /music /downloads
    chown "$PUID:$PGID" /config /data /data/media/music /data/downloads /music /downloads 2>/dev/null || true
    # A recursive chown walks every file under the volume; on a large /data (music library) that alone
    # can take minutes. The fast path below only looks two levels deep, which misses deeper mis-owned
    # files (e.g. root-owned album files from an older root-run container), so one full recursive pass
    # runs the first time (marker absent) and whenever FORCE_CHOWN=1 is set; afterwards only the fast path.
    CHOWN_MARKER=/config/.trackseerr-chown-v1
    ERR_FILE=$(mktemp)
    FULL_CHOWN_REASON=""
    if [ "${FORCE_CHOWN:-0}" = "1" ]; then
        FULL_CHOWN_REASON="FORCE_CHOWN=1"
    elif [ ! -e "$CHOWN_MARKER" ]; then
        FULL_CHOWN_REASON="first run (no $CHOWN_MARKER marker)"
    fi

    if [ -n "$FULL_CHOWN_REASON" ]; then
        echo "[entrypoint] full recursive ownership fix to $PUID:$PGID ($FULL_CHOWN_REASON; can be slow on large volumes)..."
        FULL_CHOWN_OK=1
        for dir in /config /data; do
            if chown -R "$PUID:$PGID" "$dir" 2>"$ERR_FILE"; then
                echo "[entrypoint] ownership of $dir done"
            else
                FULL_CHOWN_OK=0
                echo "[entrypoint] WARNING: chown -R on $dir reported errors: $(head -n 3 "$ERR_FILE" | tr '\n' ' ')" >&2
            fi
        done
        if [ "$FULL_CHOWN_OK" = "1" ]; then
            if touch "$CHOWN_MARKER" 2>"$ERR_FILE" && chown "$PUID:$PGID" "$CHOWN_MARKER" 2>"$ERR_FILE"; then
                echo "[entrypoint] wrote $CHOWN_MARKER (set FORCE_CHOWN=1 to repeat the full pass)"
            else
                echo "[entrypoint] WARNING: could not write $CHOWN_MARKER: $(head -n 1 "$ERR_FILE")" >&2
            fi
        else
            echo "[entrypoint] WARNING: full ownership fix incomplete; marker not written, it will be retried on next start" >&2
        fi
    else
        for dir in /config /data; do
            MISOWNED=$(find "$dir" -maxdepth 2 \( \! -uid "$PUID" -o \! -gid "$PGID" \) -print -quit 2>"$ERR_FILE" || true)
            if [ -s "$ERR_FILE" ]; then
                echo "[entrypoint] WARNING: ownership check on $dir reported errors: $(head -n 3 "$ERR_FILE" | tr '\n' ' ')" >&2
            fi
            if [ -n "$MISOWNED" ]; then
                echo "[entrypoint] fixing ownership of $dir to $PUID:$PGID (mis-owned files found, can be slow on large volumes)..."
                if chown -R "$PUID:$PGID" "$dir" 2>"$ERR_FILE"; then
                    echo "[entrypoint] ownership of $dir done"
                else
                    echo "[entrypoint] WARNING: chown -R on $dir reported errors: $(head -n 3 "$ERR_FILE" | tr '\n' ' ')" >&2
                fi
            fi
        done
    fi
    rm -f "$ERR_FILE"

    echo "[entrypoint] starting as $PUID:$PGID"
    exec gosu "$PUID:$PGID" "$@"
fi

exec "$@"
