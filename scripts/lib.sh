# Shared helpers for seed/smoke scripts. Sourced, not executed.
# Host requirements: bash, curl, base64, docker compose v2.

: "${WACS_HTTP_PORT:=3000}"
: "${HASURA_PORT:=8080}"
: "${LAPI_CONTROLLER_PORT:=7072}"
: "${WACS_ADMIN_USER:=fdbadmin}"
: "${WACS_ADMIN_PASSWORD:=fdbadmin123}"
: "${DEMO_ORG:=fielddata}"
: "${DEMO_REPO:=en_ulb}"

WACS_URL="http://localhost:${WACS_HTTP_PORT}"
HASURA_URL="http://localhost:${HASURA_PORT}"
LAPI_URL="http://localhost:${LAPI_CONTROLLER_PORT}"
SRP_HOOK_URL="http://srp-webhook/webhook"
FIXTURE_DIR="${FIXTURE_DIR:-seed/fixtures/${DEMO_REPO}}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# docker compose with the same file set the Makefile uses (COMPOSE_FILE_ARGS is exported by make).
compose() { docker compose ${COMPOSE_FILE_ARGS:-} "$@"; }

# gitea_api METHOD PATH [JSON_BODY] -> prints response body; non-2xx exits non-zero.
gitea_api() {
  local method=$1 path=$2 body=${3:-}
  local args=(-fsS -X "$method" -u "${WACS_ADMIN_USER}:${WACS_ADMIN_PASSWORD}" -H 'content-type: application/json')
  [[ -n $body ]] && args+=(--data "$body")
  curl "${args[@]}" "${WACS_URL}/api/v1${path}"
}

# Escape a string for embedding in JSON.
json_str() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

# Commit every file under $FIXTURE_DIR (create or update) to the demo repo in one commit,
# plus any extra "path=content" pairs given after the message. Fires a Gitea push webhook.
push_fixture() {
  local message=$1; shift
  local files=() rel sha op content
  local -A extra=()
  for kv in "$@"; do extra[${kv%%=*}]=${kv#*=}; done

  _entry() { # path base64content
    sha=$(gitea_api GET "/repos/${DEMO_ORG}/${DEMO_REPO}/contents/$1" 2>/dev/null \
      | grep -o '"sha":"[0-9a-f]*"' | head -1 | cut -d'"' -f4 || true)
    if [[ -n $sha ]]; then op=update; else op=create; fi
    files+=("{\"operation\":\"$op\",\"path\":$(json_str "$1"),\"content\":\"$2\"${sha:+,\"sha\":\"$sha\"}}")
  }
  while IFS= read -r -d '' f; do
    rel=${f#"$FIXTURE_DIR"/}
    _entry "$rel" "$(base64 -w0 < "$f")"
  done < <(find "$FIXTURE_DIR" -type f -print0 | sort -z)
  for rel in "${!extra[@]}"; do
    _entry "$rel" "$(printf '%s' "${extra[$rel]}" | base64 -w0)"
  done

  local IFS=,
  gitea_api POST "/repos/${DEMO_ORG}/${DEMO_REPO}/contents" \
    "{\"branch\":\"master\",\"message\":$(json_str "$message"),\"files\":[${files[*]}]}" >/dev/null
}
