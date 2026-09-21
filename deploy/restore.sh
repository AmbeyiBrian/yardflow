#!/usr/bin/env bash
#
# Restore the database from a backup (N-5, design §12.2).
#
#   ./deploy/restore.sh yardflow-20260919T020000Z.sql.gz
#   ./deploy/restore.sh --latest
#
# This exists to be *run*, not to be available. N-5 asks for a documented
# restore drill, and a drill means somebody has actually done this once on
# purpose — a backup whose restore has never been tried is a guess.
#
# It destroys the current contents of the database. It asks first.

set -euo pipefail

cd "$(dirname "$0")"

# shellcheck disable=SC1091
set -a; source .env; set +a

: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET must be set}"

if [[ "${1:-}" == "--latest" ]]; then
  key=$(aws s3 ls "s3://${BACKUP_S3_BUCKET}/database/" | sort | tail -1 | awk '{print $4}')
  [[ -n "$key" ]] || { echo "No backups found." >&2; exit 1; }
elif [[ -n "${1:-}" ]]; then
  key="$1"
else
  echo "Usage: $0 <backup-name>|--latest" >&2
  exit 1
fi

echo "About to restore ${key} over the CURRENT database."
echo "Everything in ${POSTGRES_DB} now will be replaced."
read -r -p "Type the database name to confirm: " typed
[[ "$typed" == "$POSTGRES_DB" ]] || { echo "Not confirmed. Nothing changed."; exit 1; }

tmp="/tmp/${key}"
aws s3 cp "s3://${BACKUP_S3_BUCKET}/database/${key}" "$tmp"

# The app is stopped first. Restoring under a running app would have it writing
# into a schema being dropped and rebuilt underneath it.
echo "Stopping the application (the database stays up)..."
docker compose --env-file .env stop web worker

gunzip -c "$tmp" \
  | docker compose --env-file .env exec -T postgres \
      psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=on

rm -f "$tmp"

# Migrations after the restore: the dump is from whatever version was running
# when it was taken, which may be older than the code now on the box.
docker compose --env-file .env run --rm web python manage.py migrate --noinput

echo "Starting the application..."
docker compose --env-file .env up -d

echo "Restored from ${key}."
