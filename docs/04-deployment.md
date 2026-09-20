# Deployment — the small production (§12.2)

Written for: whoever is standing up or operating the box. It assumes shell
access and an AWS console, not familiarity with this codebase.

This is **not** the deployment in design §12.1. That one — ECS Fargate, ALB,
RDS, ElastiCache, CloudFront — is the target once there are tenants who would
notice an outage. This one puts everything on a single Lightsail instance for
roughly a tenth of the cost, and is honest about what that buys and what it
costs.

| | §12.2 (this) | §12.1 (later) |
|---|---|---|
| Cost | ~$12/month | ~$200/month |
| Deploy | a few seconds of downtime | rolling, none |
| Box failure | an outage until it is rebuilt | another task takes over |
| Database failure | restore from last night's dump | point-in-time recovery |
| Scaling | one box | add tasks |

The one thing not traded away is the backup. A single box means a single point
of failure, so the database is dumped off the box every night — because
rebuilding a machine is an afternoon and losing the records is the business.

---

## 1. What lives where

Two AWS accounts, and only one of them does any work.

**Account A** owns `buniva.co.ke`. It contributes two DNS records and is then
finished:

```
yardflow.buniva.co.ke.      A   <static IP>
*.yardflow.buniva.co.ke.    A   <static IP>
```

**Account B** holds everything else: the Lightsail instance, the S3 buckets, SES,
and the IAM user whose keys go in `.env`.

There is deliberately **no cross-account access**. Certificates are issued
per-hostname over HTTP-01, so nothing on the box needs Route 53 credentials from
account A. A wildcard certificate would have required them.

### Why the wildcard DNS record

A tenant *is* a subdomain — [`core/middleware.py`](../backend/core/middleware.py)
resolves the organization from the Host header and has no production fallback.
So `silvertech.yardflow.buniva.co.ke` must resolve, and so must the next tenant's
name, which does not exist yet. A wildcard record means adding a tenant is one
command on the box and no DNS change at all.

### Why certificates are issued on demand

The wildcard record means *every* name under `yardflow.buniva.co.ke` reaches the
box, including ones nobody has ever registered. Caddy is configured to obtain a
certificate the first time a hostname is asked for — but left ungated, a few
thousand requests to random subdomains would become a few thousand certificate
requests, and Let's Encrypt would rate-limit the whole domain. The next real
tenant would then be unable to get HTTPS.

So Caddy asks first: `GET /internal/tls-allowed?domain=…`, answered by
[`config/urls.py`](../backend/config/urls.py), which says yes only for a slug
that belongs to an organization. It is unauthenticated because it is asked
during the handshake, before any session exists; it discloses only whether a
slug is in use, which the login page at that address discloses anyway.

---

## 2. Standing it up

### 2.1 The instance

Lightsail, Ubuntu 24.04, **2 GB / 2 vCPU**, in `eu-west-1`.

512 MB and 1 GB are false economies here: Postgres, Redis, gunicorn and two
Celery processes on 512 MB will be killed by the OOM reaper the first time
somebody runs a report.

Attach a **static IP** — a Lightsail instance's default address changes when it
is stopped, and the DNS records point at it.

Open **80**, **443** and **22**. Postgres is not published to the host at all —
it is reachable only from the compose network, so the box never exposes a
database to the internet.

SSH is open to the world rather than to one address because the CI runner that
deploys has no fixed IP, and GitHub's published ranges are far too many for a
Lightsail firewall to hold. That is acceptable **only because password
authentication is off** on this image (`passwordauthentication no`,
`pubkeyauthentication yes`) — brute force cannot succeed against a key. If
password auth is ever enabled, this rule must be narrowed the same day.

### 2.2 Its dependencies

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git awscli
sudo usermod -aG docker ubuntu     # log out and back in for this to take effect
```

No Node, and no build tooling. The box only pulls images — building a Vite
bundle on a 2 GB instance that is also running Postgres is how the kernel comes
to kill the database.

### 2.3 S3 and IAM, in account B

Two buckets, **both private, no public access**:

* `yardflow-attachments` — photographs and documents (N-7). Versioning on.
* `yardflow-backups` — nightly database dumps. A lifecycle rule to expire
  objects after 30 days, matching `BACKUP_RETENTION_DAYS`.

One IAM user with access to those two buckets and to SES, nothing else. Its keys
go in `.env`.

> Attachments go to S3 rather than to the instance disk on purpose. The disk is
> small, and a Lightsail snapshot of it is not a backup of somebody's evidence.

### 2.4 DNS, in account A

The two records above, pointing at the static IP. Confirm before going further —
certificates cannot be issued until the name resolves to this box:

```bash
dig +short silvertech.yardflow.buniva.co.ke
```

### 2.5 The code and the configuration

```bash
git clone https://github.com/AmbeyiBrian/yardflow.git ~/yardflow
cd ~/yardflow/deploy
cp .env.example .env
chmod 600 .env
```

Fill in `.env`. Generate the two secrets rather than inventing them:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # DJANGO_SECRET_KEY
openssl rand -base64 32                                          # POSTGRES_PASSWORD
```

`DATABASE_URL` must carry the same password as `POSTGRES_PASSWORD`. Production
settings refuse to boot on a missing value, so an incomplete file fails loudly
at start rather than quietly later.

### 2.6 Start it

```bash
cd ~/yardflow
chmod +x deploy/*.sh
# The images are private until the first CI run has pushed them, so the very
# first pull needs a login. After this, CI supplies its own short-lived token.
echo <a PAT with read:packages> | docker login ghcr.io -u AmbeyiBrian --password-stdin
./deploy/deploy.sh
docker logout ghcr.io
```

The first request to a hostname takes a few seconds while its certificate is
obtained. That is normal and happens once per tenant.

### 2.7 The first tenant

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env \
  run --rm web python manage.py provision_tenant \
    --name "Silvertech Networks Limited" \
    --slug silvertech \
    --owner-email you@yourdomain \
    --owner-name "Your Name"
```

It prints the address and a link to set the owner's password. **Use the printed
link.** Until SES is out of its sandbox, mail to an unverified address is
accepted and silently discarded, so the invitation may never arrive.

No password is ever generated for the owner (A1) — the account is created
unusable and the link is the only way in.

> `seed_demo` is **not** used here and refuses to run with `DEBUG` off. Every
> account it creates shares a password that is written in this repository, which
> is fine on a laptop and an open door on a public address.

---

## 3. Deploying a change

**Push to `main`.** That is the whole procedure.

```
push to main
  └─ tests (backend, frontend)          ✗ → stops here
       └─ build both images → ghcr.io
            └─ ssh to the box: pull, migrate, restart
```

Tests gate the images and the images gate the deploy, so a red build never
reaches the box and a failed push never leaves it pulling a tag that does not
exist. About two minutes end to end, of which a few seconds are downtime while
the web container restarts.

Every build is tagged with its commit as well as `latest`, so a rollback names a
specific build rather than "whatever `latest` used to be":

```bash
ssh ubuntu@<static IP>
cd ~/yardflow && git checkout <good sha>
export BACKEND_IMAGE=ghcr.io/ambeyibrian/yardflow-backend:<good sha>
export WEB_IMAGE=ghcr.io/ambeyibrian/yardflow-web:<good sha>
./deploy/deploy.sh
```

> A rollback runs the **old** code against the **new** schema. Django migrations
> are forward-only, so this is safe for a release that added a column and not for
> one that dropped or renamed anything. Where a migration was destructive, the
> way back is `restore.sh`, not a tag.

### 3.1 What CI needs from you, once

The deploy job is **skipped** until a repository *variable* `DEPLOY_ENABLED` is
set to `true`. Until the box exists there is nothing to deploy to, and a
pipeline that goes red because infrastructure has not been built yet teaches
everybody to ignore a red pipeline.

Then four repository secrets, under Settings → Secrets and variables → Actions:

| Secret | What it is |
|---|---|
| `DEPLOY_HOST` | The static IP |
| `DEPLOY_USER` | `ubuntu` |
| `DEPLOY_SSH_KEY` | Private half of a key **made for this**, not your personal one |
| `DEPLOY_KNOWN_HOSTS` | Output of `ssh-keyscan <static IP>` |

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/yardflow_deploy -N ""
ssh-copy-id -i ~/.ssh/yardflow_deploy.pub ubuntu@<static IP>
ssh-keyscan <static IP>            # paste into DEPLOY_KNOWN_HOSTS
cat ~/.ssh/yardflow_deploy         # paste into DEPLOY_SSH_KEY
```

`DEPLOY_KNOWN_HOSTS` is not optional ceremony: without a pinned host key the
deploy step would trust whatever answers on that address.

Nothing else is stored. The box authenticates to the registry with a token
minted for that one workflow run and logs out afterwards, so there is no
standing credential on the instance.

---

## 4. Backups, and the restore drill

Nightly, by cron:

```
0 2 * * * /home/ubuntu/yardflow/deploy/backup.sh >> /var/log/yardflow-backup.log 2>&1
```

`backup.sh` refuses to upload a dump under 10 KB — `pg_dump | gzip` reports
success when `gzip` succeeds even if `pg_dump` failed, so without that check a
broken backup looks exactly like a working one until the day it matters.

Also enable **Lightsail automatic snapshots**. They cover the whole box, which is
faster to rebuild from; the S3 dump covers the case where the box and its
snapshots are both gone.

### N-5 is not satisfied until a restore has been done

```bash
./deploy/restore.sh --latest
```

Run it once, deliberately, before the tenant has data worth keeping. A backup
whose restore has never been tried is a guess, and the drill is part of the
definition of done.

---

## 5. Operating it

```bash
cd ~/yardflow/deploy
docker compose --env-file .env ps
docker compose --env-file .env logs -f web
docker compose --env-file .env logs -f caddy      # certificate problems live here
```

**A tenant's HTTPS is failing.** Ask the gate what it would answer:

```bash
curl -s "http://localhost:8000/internal/tls-allowed?domain=slug.yardflow.buniva.co.ke"
```

A 403 means no organization has that slug — the certificate was correctly
refused. Check the spelling before anything else.

**Ledger verification** runs nightly under Celery beat (§13). Its alerts are in
the worker log. It never auto-corrects; drift is reported and investigated.

---

## 6. What this deployment does not do

Stated so nobody discovers it during an incident.

* **No redundancy.** One box. If it fails, the system is down until it is
  rebuilt and restored.
* **No zero-downtime deploys.** A few seconds each time.
* **No point-in-time recovery.** The most that can be lost is one day's work,
  bounded by when the nightly dump ran.
* **No autoscaling.** A report that pins both vCPUs will slow everything else
  down.
* **Logs are on the box.** They are lost with it. CloudWatch shipping arrives
  with §12.1.

Each of these is answered by §12.1. Move when a tenant would notice, not before.
