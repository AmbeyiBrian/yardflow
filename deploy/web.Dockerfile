# Caddy with the built SPA inside it (design §12.2).
#
# The frontend used to be built on the instance. That was wrong for a 2 GB box
# that is also running Postgres, Redis and two Celery processes: `npm ci` and a
# Vite build peak well over a gigabyte, and the first thing the kernel kills
# under pressure is whatever is largest — often the database.
#
# Built in CI instead, and shipped as an image. The box pulls. That also means
# the bundle that was tested is the bundle that is served, rather than one built
# again on a different machine with a different lockfile resolution.
#
# Build context is the repository root: it needs `frontend/` and `deploy/`.

FROM node:22-alpine AS build

WORKDIR /build

# Lockfile first, so a change to a component does not reinstall the world.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


FROM caddy:2-alpine

# The configuration ships with the image rather than being mounted from the
# box. It is code — it decides what gets a certificate and what reaches Django —
# and a file edited in place on a server is a change nobody can review.
COPY deploy/Caddyfile /etc/caddy/Caddyfile

COPY --from=build /build/dist /srv/www

EXPOSE 80 443
