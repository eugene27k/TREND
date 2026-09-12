#!/bin/sh
# Restore from the replica before the first start (a fresh container on the free
# host has an empty volume), then run the engine under Litestream so the WAL is
# streamed continuously. Without Litestream the engine simply runs directly.
set -eu

DB_PATH="${AEGIS_STORAGE__DB_PATH:-/app/data/trend.db}"
REPLICA="${AEGIS_BACKUP__LITESTREAM_REPLICA_PATH:-}"

if command -v litestream >/dev/null 2>&1 && [ -n "$REPLICA" ]; then
    if [ ! -f "$DB_PATH" ]; then
        echo "entrypoint: no local database, attempting restore from $REPLICA"
        litestream restore -if-replica-exists -o "$DB_PATH" "$REPLICA" || \
            echo "entrypoint: no replica to restore from, starting fresh"
    fi
    exec litestream replicate -exec "python -m engine $*" "$DB_PATH" "$REPLICA"
fi

echo "entrypoint: litestream unavailable or unconfigured, running without replication"
exec python -m engine "$@"
