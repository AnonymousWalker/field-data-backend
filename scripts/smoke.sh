#!/usr/bin/env bash
# Smoke test for the local stack (run after `make up && make seed`):
#   1. every service answers its health endpoint
#   2. end-to-end: a new commit to the demo repo is rendered by SRP into Azurite and recorded by
#      languageapi (visible through Hasura)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source scripts/lib.sh

TIMEOUT=${SMOKE_TIMEOUT:-180}
BLOB_URL="http://localhost:${AZURITE_BLOB_PORT:-10000}/devstoreaccount1/\$web/u/${DEMO_ORG}/${DEMO_REPO}"
# Hasura objects known to be inconsistent because upstream defines them outside the migrations.
KNOWN_INCONSISTENT='vw_consolidated_repos|Consolidated Repos'

fail() {
  printf '\033[1;31mSMOKE FAILED:\033[0m %s\n' "$*" >&2
  echo "--- recent logs (srp-worker, lapi-controller) ---" >&2
  compose logs --tail 40 srp-worker lapi-controller >&2 || true
  exit 1
}
check() { # name cmd...
  if "${@:2}" >/dev/null 2>&1; then echo "  ok   $1"; else fail "$1"; fi
}
wait_for() { # description timeout cmd...
  local desc=$1 t=$2 start=$SECONDS; shift 2
  until "$@" >/dev/null 2>&1; do
    (( SECONDS - start > t )) && fail "timed out after ${t}s waiting for: ${desc}"
    sleep 3
  done
  echo "  ok   ${desc} ($(( SECONDS - start ))s)"
}
hasura() { curl -fsS "${HASURA_URL}/v1/graphql" -H 'content-type: application/json' -d "$1"; }

log "Health checks"
check "WACS /api/healthz"            curl -fsS "${WACS_URL}/api/healthz"
check "Hasura /healthz"              curl -fsS "${HASURA_URL}/healthz"
check "languageapi controller"       curl -fsS "${LAPI_URL}/api/language"
check "SRP webhook rejects garbage"  bash -c "[[ \$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:${SRP_WEBHOOK_PORT:-7071}/webhook -d '{}') == 400 ]]"
check "Service Bus emulator"         curl -fsS "http://localhost:${SERVICEBUS_MGMT_PORT:-5300}/health"
check "Azurite blob"                 bash -c "curl -s -o /dev/null http://localhost:${AZURITE_BLOB_PORT:-10000}/"
check "SRP worker running"           bash -c "[[ \$(docker compose ${COMPOSE_FILE_ARGS:-} ps --status running -q srp-worker) ]]"
inconsistent=$(curl -fsS "${HASURA_URL}/v1/metadata" -d '{"type":"get_inconsistent_metadata","args":{}}' \
  | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | grep -vE "$KNOWN_INCONSISTENT" || true)
[[ -z $inconsistent ]] || fail "unexpected inconsistent Hasura metadata: ${inconsistent}"
echo "  ok   Hasura metadata (only known upstream gaps inconsistent)"
gitea_api GET "/repos/${DEMO_ORG}/${DEMO_REPO}" >/dev/null 2>&1 || fail "demo repo missing; run 'make seed' first"

log "End-to-end: commit -> webhook -> render -> RepoRendered -> languageapi"
marker="smoke-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
started_utc=$(date -u +%Y-%m-%dT%H:%M:%S)
push_fixture "Smoke test ${marker}" "smoke/last-run.txt=${marker}"
echo "  ok   pushed commit '${marker}'"

wait_for "SRP rendered the commit (build_log.json)" "$TIMEOUT" \
  bash -c "curl -fsS '${BLOB_URL}/build_log.json' | grep -q '${marker}'"
curl -fsS "${BLOB_URL}/build_log.json" | grep -q '"success":true' || fail "SRP build_log.json reports failure"
check "rendered chapter page is served" curl -fsS "${BLOB_URL}/JUD/1.html"

query=$(cat <<EOF
{"query":"{ rendered_content(where:{_and:[{content:{name:{_eq:\"${DEMO_ORG}/${DEMO_REPO}\"}}},{modified_on:{_gte:\"${started_utc}\"}}]}) { url } }"}
EOF
)
wait_for "languageapi recorded the renderings (Hasura)" "$TIMEOUT" \
  bash -c "curl -fsS '${HASURA_URL}/v1/graphql' -H 'content-type: application/json' -d '${query}' | grep -q '\"url\"'"

log "Smoke test passed"
echo "  Rendered output: ${BLOB_URL}/JUD/1.html"
echo "  Hasura console:  ${HASURA_URL}/console"
