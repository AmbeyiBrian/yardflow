#!/usr/bin/env bash
#
# Nightly database backup to S3 (N-5, design §12.2).
#
#   0 2 * * *  /home/ubuntu/yardflow/deploy/backup.sh >> /var/log/yardflow-backup.log 2>&1
#
# The database is on the same box as the app, so a Lightsail snapshot and the
# database are the same single point of failure. This puts a dump somewhere
# else, which is the difference between an outage and a loss.
#
# **A backup nobody has restored is not a backup.** `restore.sh` is the other
# half, and §12.2 makes running it once part of the definition of done.

set -euo pipefail

cd "$(dirname "$0")"

# shellcheck disable=SC1091
set -a; source .env; set +a

: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET must be set}"
: "${POSTGRES_USER:?}"
: "${POSTGRES_DB:?}"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
name="yardflow-${stamp}.sql.gz"
tmp="/tmp/${name}"

# `--clean --if-exists`: the dump can be replayed onto a database that already
# has objects, which is what a restore onto a rebuilt box actually looks like.
docker compose --env-file .env exec -T postgres \
  pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
          --clean --if-exists --no-owner \
  | gzip -9 > "$tmp"

# An empty or tiny file means pg_dump failed while gzip succeeded — the pipe
# would otherwise report success and the backup would be junk nobody noticed
# until the restore.
size=$(stat -c%s "$tmp")
if (( size < 10000 )); then
  echo "FAILED: dump is only ${size} bytes, refusing to upload it" >&2
  rm -f "$tmp"
  exit 1
fi

aws s3 cp "$tmp" "s3://${BACKUP_S3_BUCKET}/database/${name}" \
  --storage-class STANDARD_IA
rm -f "$tmp"

echo "$(date -u +%FT%TZ) uploaded ${name} (${size} bytes)"

# Retention. The bucket should also have a lifecycle rule doing this, but a
# rule somebody forgot to add is a bucket that grows forever.
cutoff=$(date -u -d "${BACKUP_RETENTION_DAYS:-30} days ago" +%Y%m%d)
aws s3 ls "s3://${BACKUP_S3_BUCKET}/database/" \
  | awk '{print $4}' \
  | while read -r key; do
      [[ "$key" =~ ^yardflow-([0-9]{8})T ]] || continue
      if [[ "${BASH_REMATCH[1]}" < "$cutoff" ]]; then
        aws s3 rm "s3://${BACKUP_S3_BUCKET}/database/${key}"
        echo "removed ${key}"
      fi
    done
