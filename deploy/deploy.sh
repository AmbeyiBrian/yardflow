#!/usr/bin/env bash
#
# Deploy, on the box (design §12.2).
#
# Called by CI over SSH after the images are built and pushed, and runnable by
# hand for a rollback or a first run:
#
#   ./deploy/deploy.sh                      # whatever :latest points at
#   BACKEND_IMAGE=...:sha-abc123 \
#   WEB_IMAGE=...:sha-abc123 ./deploy/deploy.sh    # a specific build
#
# Nothing is built here. The box pulls what CI produced, so the artifact that
# was tested is the artifact that runs.
#
# Migrations run in a **one-off container before** the services restart, so a
# migration that fails leaves the old version serving rather than a new version
# addressing a schema it does not understand.

set -euo pipefail

cd "$(dirname "$0")"

COMPOSE=(docker compose --env-file .env)

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ -f .env ]] || { echo "deploy/.env is missing. Copy .env.example and fill it in." >&2; exit 1; }

say "Pulling images"
"${COMPOSE[@]}" pull

say "Starting the database"
"${COMPOSE[@]}" up -d postgres redis

say "Migrating"
# `run --rm` is a one-off container, not the running service. A non-zero exit
# stops the script here and the old containers are still serving.
"${COMPOSE[@]}" run --rm web python manage.py migrate --noinput

say "Starting everything"
"${COMPOSE[@]}" up -d --remove-orphans

# Every container, not just the web one. The first deployment reported success
# while Caddy was in a crash loop, because only `web` was being watched — so the
# site was entirely unreachable and the script said "Deployed."
say "Checking nothing is restarting"
sleep 15
if "${COMPOSE[@]}" ps --format '{{.Service}} {{.Status}}' | grep -Ei 'restarting|exited'; then
  echo "A container is not staying up. Logs:" >&2
  "${COMPOSE[@]}" logs --tail 40 >&2
  exit 1
fi

say "Waiting for the web container to report healthy"
for _ in $(seq 1 45); do
  status=$("${COMPOSE[@]}" ps --format json web 2>/dev/null \
    | grep -o '"Health":"[a-z]*"' | head -1 | cut -d'"' -f4 || true)
  if [[ "$status" == "healthy" ]]; then
    # Record what is now running, so a later manual restart uses it.
    #
    # CI pins both images by commit and passes them in the environment. That
    # environment is gone the moment this script exits, and the compose file's
    # fallback is `:latest` — which on this box is whatever was pulled *first*,
    # because CI never pulls `latest` again. So a hand-run
    # `docker compose up -d --force-recreate web` to pick up a changed .env
    # quietly rolled the worker back to the very first build. Writing the
    # pinned tags into .env makes the deployed build the default instead.
    if [[ -n "${BACKEND_IMAGE:-}" ]]; then
      sed -i '/^BACKEND_IMAGE=/d;/^WEB_IMAGE=/d' .env
      printf 'BACKEND_IMAGE=%s
WEB_IMAGE=%s
' "$BACKEND_IMAGE" "${WEB_IMAGE:-}" >> .env
    fi
    say "Deployed."
    "${COMPOSE[@]}" ps
    # Old image layers accumulate a gigabyte at a time on a 40 GB disk.
    docker image prune --force --filter "until=168h" >/dev/null 2>&1 || true
    exit 0
  fi
  sleep 2
done

echo "The web container did not become healthy. Recent logs:" >&2
"${COMPOSE[@]}" logs --tail 60 web >&2
exit 1
