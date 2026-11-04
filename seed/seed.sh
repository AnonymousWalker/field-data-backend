#!/usr/bin/env bash
# Idempotent seeding for the local stack. Safe to re-run; run after `make up`.
#   1. WACS: admin user, token-scope trigger, system webhook -> SRP
#   2. Azurite: SRP output/template containers + print template
#   3. languageapi: minimal reference data (region, country, language "en")
#   4. WACS: demo org + Bible repo from seed/fixtures (its commit fires the webhook -> render)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source scripts/lib.sh

log "Seeding WACS admin user (${WACS_ADMIN_USER})"
out=$(compose exec -T wacs gitea --config /config/app.ini admin user create \
  --username "$WACS_ADMIN_USER" --password "$WACS_ADMIN_PASSWORD" \
  --email "${WACS_ADMIN_USER}@example.com" --admin --must-change-password=false 2>&1) \
  || grep -q "already exists" <<<"$out" || { echo "$out"; die "could not create admin user"; }

log "Applying WACS access-token scope trigger"
compose exec -T wacs-db mariadb -uroot -pgitea gitea < seed/sql/wacs-token-trigger.sql

log "Registering system webhook WACS -> SRP (${SRP_HOOK_URL})"
if gitea_api GET /admin/hooks | grep -q "\"url\":\"${SRP_HOOK_URL}\""; then
  echo "  already registered"
else
  gitea_api POST /admin/hooks "$(cat <<EOF
{"type":"gitea","active":true,"events":["push","create","repository"],
 "config":{"url":"${SRP_HOOK_URL}","content_type":"json"}}
EOF
)" >/dev/null
fi

log "Preparing Azurite containers for SRP"
compose run --rm -T tools sh -c '
  set -e
  for c in "\$web" template versecounts; do az storage container create --name "$c" -o none; done
  # Rendered output is publicly readable, like the production static site, so pages open in a browser.
  az storage container set-permission --name "\$web" --public-access blob -o none
  az storage blob upload --overwrite -c template -f /seed/templates/print.html -n print.html -o none
'

log "Seeding languageapi reference data"
for f in region country language; do
  resp=$(curl -fsS -X POST "${LAPI_URL}/api/${f}" -H 'content-type: application/json' --data "@seed/languageapi/${f}.json")
  grep -q '"ok":true' <<<"$resp" || die "languageapi ${f} seed failed: ${resp}"
done

log "Creating demo org/repo ${DEMO_ORG}/${DEMO_REPO}"
gitea_api GET "/orgs/${DEMO_ORG}" >/dev/null 2>&1 \
  || gitea_api POST /orgs "{\"username\":\"${DEMO_ORG}\",\"visibility\":\"public\"}" >/dev/null
if gitea_api GET "/repos/${DEMO_ORG}/${DEMO_REPO}" >/dev/null 2>&1; then
  echo "  repo exists; use 'make smoke' to push a new commit and re-render"
else
  gitea_api POST "/orgs/${DEMO_ORG}/repos" \
    "{\"name\":\"${DEMO_REPO}\",\"default_branch\":\"master\",\"auto_init\":false}" >/dev/null
  push_fixture "Seed ${DEMO_REPO} from seed/fixtures"
fi

log "Seed complete. WACS: ${WACS_URL}/${DEMO_ORG}/${DEMO_REPO} (login ${WACS_ADMIN_USER} / ${WACS_ADMIN_PASSWORD})"
