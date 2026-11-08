#!/usr/bin/env bash
# Hardening and runtime of the apps server (Ubuntu 24.04). Idempotent; run as root:
#   bootstrap.sh "<public key that deploys stand>" "<public key that deploys prod>"
# Afterwards: keys only (root by key, no passwords), ufw 22/80/443, fail2ban on sshd, security updates
# on their own, Docker with rotated logs, user `deploy` that can do nothing but run apps-deploy.
set -euo pipefail
STAND_KEY="${1:?public key of the stand deploy}"
PROD_KEY="${2:?public key of the prod deploy}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive

apt-get update -q
apt-get install -yq ca-certificates curl jq skopeo ufw fail2ban unattended-upgrades

# --- Docker from the official repository ---
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
install -m 0644 "$HERE/daemon.json" /etc/docker/daemon.json
systemctl restart docker
for n in edge-prod edge-stand edge-odoo; do docker network inspect "$n" >/dev/null 2>&1 || docker network create "$n"; done

# --- ssh: keys only ---
install -m 0644 "$HERE/sshd-alnuzum.conf" /etc/ssh/sshd_config.d/10-alnuzum.conf
sshd -t
systemctl reload ssh

# --- firewall: published Docker ports bypass ufw, so only Caddy publishes any (80, 443) ---
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# --- fail2ban on sshd (journal backend on 24.04) ---
install -m 0644 "$HERE/jail-sshd.local" /etc/fail2ban/jail.d/sshd.local
systemctl enable --now fail2ban
systemctl restart fail2ban

# --- security updates on their own, reboot at night only when one requires it ---
install -m 0644 "$HERE/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades
install -m 0644 "$HERE/52alnuzum-unattended" /etc/apt/apt.conf.d/52alnuzum-unattended
systemctl enable --now unattended-upgrades

install -d -m 0755 /srv/apps

# --- deploy users: one per environment, no password, one key each, forced into apps-deploy ---
if id deploy >/dev/null 2>&1; then pkill -u deploy || true; userdel -f -r deploy; fi
for env in stand prod; do
  user="deploy-$env"
  key="$STAND_KEY"; [ "$env" = prod ] && key="$PROD_KEY"
  id "$user" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$user"
  usermod -aG docker "$user"
  passwd -l "$user" >/dev/null
  install -d -m 0700 -o "$user" -g "$user" "/home/$user/.ssh"
  echo "restrict,command=\"/usr/local/bin/apps-deploy $env\" $key" > "/home/$user/.ssh/authorized_keys"
  chown "$user:$user" "/home/$user/.ssh/authorized_keys"
  chmod 0600 "/home/$user/.ssh/authorized_keys"
  # the other environment's user cannot read this one's secrets
  install -d -m 0700 -o "$user" -g "$user" "/srv/apps/$env"
done

# --- layout ---
install -d -m 0755 -o root -g root /srv/apps /srv/apps/edge
install -d -m 0750 /etc/alnuzum /var/lib/alnuzum
install -m 0755 "$HERE/apps-deploy" /usr/local/bin/apps-deploy
install -m 0644 "$HERE/compose.app.yml" /srv/apps/compose.app.yml
install -m 0644 "$HERE/compose.edge.yml" /srv/apps/edge/compose.yml
install -m 0644 "$HERE/Caddyfile" /srv/apps/edge/Caddyfile
install -d -m 0755 /var/log/caddy
docker compose -p edge -f /srv/apps/edge/compose.yml up -d
echo "bootstrap done"
