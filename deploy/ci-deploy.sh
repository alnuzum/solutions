#!/usr/bin/env bash
# Run by CI after the gate: build the image of this commit, hand the environment's secrets and the
# image to the server (deploy-<env>, forced command apps-deploy), start it there.
#   ci-deploy.sh stand|prod <sha>
# Needs DEPLOY_HOST, DEPLOY_SSH_KEY, DEPLOY_KNOWN_HOSTS and the app secrets in the environment.
set -euo pipefail
ENV_NAME="${1:?stand or prod}"
SHA="${2:?commit sha}"
: "${DEPLOY_HOST:?}" "${DEPLOY_SSH_KEY:?}" "${DEPLOY_KNOWN_HOSTS:?}"
umask 077
dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$dir/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$dir/known_hosts"
# a malformed variable reads as "Host key verification failed"; say what is wrong instead
ssh-keygen -F "$DEPLOY_HOST" -f "$dir/known_hosts" >/dev/null ||
  { echo "DEPLOY_KNOWN_HOSTS has no key line for $DEPLOY_HOST (want: '${DEPLOY_HOST},<ip>... <type> <key>')" >&2; exit 1; }
remote() {
  ssh -i "$dir/key" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$dir/known_hosts" -o ServerAliveInterval=15 "deploy-$ENV_NAME@$DEPLOY_HOST" "$@"
}

docker build -q -t "platform:$SHA" . >/dev/null
for key in POSTGRES_PASSWORD SECRET_KEYS SESSION_SECRET ADMIN_TOKEN KOMMO_CLIENT_ID KOMMO_CLIENT_SECRET \
  ZOHO_CLIENT_ID ZOHO_CLIENT_SECRET QUOTA_WRITES_PER_DAY \
  ANTHROPIC_API_KEY ORDERS_MODEL_USD_PER_DAY ORDERS_MODEL_CALLS_PER_DAY ORDERS_RUNS_PER_SESSION ORDERS_RUNS_PER_IP \
  ORDERS_DEMO_WRITES_PER_SESSION DEMO_AMOCRM_DOMAIN DEMO_AMOCRM_TOKEN DEMO_KOMMO_DOMAIN DEMO_KOMMO_TOKEN \
  DEMO_B24_WEBHOOK DEMO_ZOHO_REFRESH_TOKEN DEMO_ODOO_URL DEMO_ODOO_DB DEMO_ODOO_LOGIN DEMO_ODOO_KEY \
  ELEVENLABS_API_KEY VOICE_USD_PER_DAY VOICE_TG_TOKEN VOICE_TG_SECRET VOICE_TG_BOT; do
  if [ -n "${!key:-}" ]; then printf '%s=%s\n' "$key" "${!key}"; fi
done > "$dir/app.env"
remote "env $SHA" < "$dir/app.env"
docker save "platform:$SHA" | remote "image $SHA"
remote "deploy $SHA"
