#!/bin/sh
set -eu
umask 077

PROJECT_DIR=${SHOWROOMFLOW_PROJECT_DIR:-/opt/showroomflow}
BACKUP_DIR=${SHOWROOMFLOW_BACKUP_DIR:-/var/backups/showroomflow}
RETENTION_DAYS=${SHOWROOMFLOW_BACKUP_RETENTION_DAYS:-14}
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$BACKUP_DIR"

docker compose \
    --project-directory "$PROJECT_DIR" \
    --env-file "$PROJECT_DIR/.env.production" \
    -f "$PROJECT_DIR/compose.production.yaml" \
    exec -T db sh -c 'pg_dump --format=custom --username="$POSTGRES_USER" "$POSTGRES_DB"' \
    > "$BACKUP_DIR/showroomflow-$TIMESTAMP.dump"

find "$BACKUP_DIR" -type f -name 'showroomflow-*.dump' -mtime "+$RETENTION_DAYS" -delete
