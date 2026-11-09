# Deploy and operations

One server (Ubuntu 24.04) runs production (`apps.alnuzum.com`), the stand (`apps-stand.alnuzum.com`) and a
test Odoo Community (`odoo-e2e.alnuzum.com`) behind one Caddy. All three names are A and AAAA records
pointing at the server; Caddy gets their certificates itself.

| what | where |
|---|---|
| hardening, Docker, Caddy, users, timers | `server/bootstrap.sh`, run by root; deploys never change these files |
| an environment | compose project `apps-<env>` from `/srv/apps/compose.app.yml`, its own Postgres, no published ports |
| secrets of an environment | GitHub environments `stand` and `production`; on the server `/srv/apps/<env>/.env` (0600, owner `deploy-<env>`) |
| test Odoo | compose project `odoo-e2e` from `/srv/apps/compose.odoo.yml`; `/srv/apps/odoo/.env` (0600) for its Postgres, `/srv/apps/odoo/odoo.conf` (0600, owner uid 100 of the image, directory 0700 root) for Odoo, so the password is not a command-line argument |
| edge | Caddy: hooks take at most 64 KB, an order with its file (`POST /{ru,en}/orders`) 9 MB, other requests 1 MB; `/connections` (admin API) answers 404 from outside |

## Deploy

CI deploys after the gate (lint, tests, compose, gitleaks): a pull request from this repository goes to
the stand, a push to `main` to production. `ci-deploy.sh` builds the image and talks to the server as
`deploy-<env>`, whose key can only run `apps-deploy <env>`: `env <sha>` (the secrets as text),
`image <sha>` (a `docker save` archive, read by skopeo into `alnuzum/platform-<env>:<sha>` only),
`deploy <sha>` (migrations in a separate container, `up --wait`, `/health` through Caddy with this
version, otherwise back to the previous one), `status`.

`DEPLOY_KNOWN_HOSTS` holds one line per host key type for the name and both addresses:
`apps.alnuzum.com,<ipv4>,<ipv6> <type> <key>`. Build it with `ssh-keyscan` and compare fingerprints with
a login you already trust.

## Watching

- From outside: `.github/workflows/uptime.yml` every five minutes, `/health` of both environments.
- On the server: `apps-watchdog.timer` every minute. It restarts unhealthy containers and messages
  Telegram when the set of problems changes: a container died or is not running, a site does not answer
  through its public address, five 5xx within five minutes, the disk at 85 %, no good dump for 26 hours.
  An environment that was never deployed is not watched. To stop a container on purpose, stop the
  timer first. State: `/var/lib/alnuzum/watchdog.state`; log: `journalctl -u apps-watchdog`.
- Telegram: the bot token and chat are the `TELEGRAM_ALERT_TOKEN` and `TELEGRAM_ALERT_CHAT` secrets of
  the repository and `/etc/alnuzum/alerts.env` (0600) on the server.

## Backups

`apps-backup.timer` at 22:30 UTC: `pg_dump -Fc` of production, piped over ssh to the backup host. There
the key of `apps-backup` works only from the server's addresses and only runs `apps-backup-receive`,
which takes a pg_dump archive of exactly the announced size and checksum, named within 15 minutes of
now, and never replaces or deletes a stored one (`/var/backups/apps`). A daily cron job on the backup
host, `apps-backup-prune`, removes dumps whose name is older than 14 days. Set the backup host up with `backup-host/setup.sh "<backup_key.pub>"
"<server addresses>"`. The database keeps serving during the dump.

Restore into a fresh Postgres and compare row counts before trusting a dump:

```bash
docker run -d --name restore -e POSTGRES_USER=alnuzum -e POSTGRES_PASSWORD=x -e POSTGRES_DB=alnuzum \
  -v "$PWD:/dump:ro" postgres:16-alpine
docker exec restore pg_restore -U alnuzum -d alnuzum --no-owner /dump/prod-<time>.dump
docker exec restore psql -U alnuzum -d alnuzum -c 'select count(*) from crm_connections'
```

The encrypted CRM secrets in a dump need the `SECRET_KEYS` of that environment, which are not in it.
