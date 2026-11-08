#!/usr/bin/env bash
# On the backup host, as root: a user that can do one thing, store a dump of production.
#   setup.sh "<public key of /etc/alnuzum/backup_key on the apps server>" "<addresses of the apps server, comma-separated>"
# The key works only from those addresses, without a shell, forwarding or a pty (restrict), and only
# runs apps-backup-receive whatever command the client asks for.
set -euo pipefail
KEY="${1:?public key}"
FROM="${2:?addresses of the apps server}"
HERE="$(cd "$(dirname "$0")" && pwd)"
id apps-backup >/dev/null 2>&1 || useradd --create-home --shell /bin/bash apps-backup
passwd -l apps-backup >/dev/null
install -m 0755 "$HERE/apps-backup-receive" /usr/local/bin/apps-backup-receive
install -m 0755 "$HERE/apps-backup-prune" /usr/local/bin/apps-backup-prune
echo "15 3 * * * apps-backup /usr/local/bin/apps-backup-prune" > /etc/cron.d/apps-backup-prune
chmod 0644 /etc/cron.d/apps-backup-prune
install -d -m 0700 -o apps-backup -g apps-backup /var/backups/apps /home/apps-backup/.ssh
echo "restrict,from=\"$FROM\",command=\"/usr/local/bin/apps-backup-receive\" $KEY" > /home/apps-backup/.ssh/authorized_keys
chown apps-backup:apps-backup /home/apps-backup/.ssh/authorized_keys
chmod 0600 /home/apps-backup/.ssh/authorized_keys
echo "apps-backup ready"
