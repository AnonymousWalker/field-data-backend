#!/usr/bin/env bash
# Offline end-to-end test of the fdb CLI (bin/fdb) and HTTP API (fdb-api). Run after `make up seed`.
#   CLI : import a throwaway git repo (file://, built from seed/fixtures), put an edit, wait for the
#         pipeline, check the rendered page.
#   HTTP: import the seeded demo repo from inside the network, put an edit, wait, check.
#   Guards: non-local WACS URLs are refused; imported mirrors can't push to their source.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source scripts/lib.sh

API="http://127.0.0.1:${FDB_API_PORT:-7080}"
BLOB="http://localhost:${AZURITE_BLOB_PORT:-10000}/devstoreaccount1/\$web/u"
RUN_ID="t$(date -u +%Y%m%d%H%M%S)"
WORK=".local/test-tools/${RUN_ID}"
mkdir -p "$WORK"

fail() { printf '\033[1;31mTEST-TOOLS FAILED:\033[0m %s\n' "$*" >&2; exit 1; }
ok() { echo "  ok   $*"; }
json_field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval('d'+sys.argv[1]))" "$1"; }

edited_jude() { # marker -> stdout: fixture Jude with the marker in verse 1
  sed "0,/\\\\v 1 /s//\\\\v 1 [$1] /" seed/fixtures/en_ulb/65-JUD.usfm
}

log "CLI: import a local git repo via file://"
SRC="$WORK/src/en_ulb"
mkdir -p "$SRC" && cp -r seed/fixtures/en_ulb/. "$SRC/"
git -C "$SRC" init -q -b master
git -C "$SRC" -c user.name=fdb-test -c user.email=fdb-test@example.com add -A
git -C "$SRC" -c user.name=fdb-test -c user.email=fdb-test@example.com commit -qm "fixture for $RUN_ID"
CLI_REPO="fdbtest/${RUN_ID}_ulb"
bin/fdb import "file://$ROOT/$SRC" --as "$CLI_REPO" --wait 300 >/dev/null \
  || fail "fdb import --wait did not complete (bin/fdb status $CLI_REPO)"
ok "imported and rendered $CLI_REPO"
[[ $(git -C ".local/imports/$CLI_REPO.git" remote get-url --push origin) == DISABLED ]] \
  || fail "imported mirror can still push to its source"
ok "mirror push URL is DISABLED"

log "CLI: put an edit and follow it through the pipeline"
MARK="CLI-$RUN_ID"
edited_jude "$MARK" > "$WORK/jud.usfm"
bin/fdb --json put "$CLI_REPO" 65-JUD.usfm "$WORK/jud.usfm" -m "test-tools $MARK" --wait 300 > "$WORK/put.json" \
  || fail "fdb put --wait did not complete: $(cat "$WORK/put.json")"
[[ $(json_field "['pipeline']['status']" < "$WORK/put.json") == ok ]] || fail "pipeline not ok"
curl -fsS "$BLOB/$CLI_REPO/JUD/1.html" | grep -q "$MARK" || fail "rendered page lacks the edit"
ok "edit rendered and recorded by languageapi ($(json_field "['pipeline']['elapsed_s']" < "$WORK/put.json")s)"

log "HTTP: import, put, pipeline via fdb-api"
curl -fsS "$API/healthz" >/dev/null || fail "fdb-api not reachable at $API"
HTTP_REPO="fdbtest-http/${RUN_ID}_ulb"
curl -fsS -X POST "$API/import" -H 'content-type: application/json' \
  -d "{\"url\":\"http://wacs:3000/${DEMO_ORG}/${DEMO_REPO}.git\",\"as\":\"$HTTP_REPO\",\"wait\":300}" > "$WORK/http-import.json" \
  || fail "POST /import failed"
[[ $(json_field "['pipeline']['status']" < "$WORK/http-import.json") == ok ]] \
  || fail "import pipeline not ok: $(cat "$WORK/http-import.json")"
ok "POST /import rendered $HTTP_REPO"
MARK="HTTP-$RUN_ID"
edited_jude "$MARK" > "$WORK/jud-http.usfm"
curl -fsS -X PUT "$API/repos/$HTTP_REPO/contents/65-JUD.usfm?message=test-tools%20$MARK&wait=300" \
  --data-binary "@$WORK/jud-http.usfm" > "$WORK/http-put.json" || fail "PUT contents failed"
[[ $(json_field "['pipeline']['status']" < "$WORK/http-put.json") == ok ]] \
  || fail "put pipeline not ok: $(cat "$WORK/http-put.json")"
curl -fsS "$BLOB/$HTTP_REPO/JUD/1.html" | grep -q "$MARK" || fail "rendered page lacks the HTTP edit"
code=$(curl -s -o /dev/null -w '%{http_code}' "$API/repos/$HTTP_REPO/pipeline")
[[ $code == 200 ]] || fail "GET pipeline returned $code"
ok "PUT edit rendered and recorded; GET pipeline = 200"

log "Guards"
if FDB_WACS_URL=https://content.bibletranslationtools.org bin/fdb list 2>"$WORK/guard.err"; then
  fail "a non-local WACS URL was accepted"
fi
grep -q "refusing" "$WORK/guard.err" || fail "unexpected guard error: $(cat "$WORK/guard.err")"
ok "non-local WACS URL refused"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/import" -d '{}')
[[ $code == 400 ]] || fail "bad import request returned $code, expected 400"
ok "bad HTTP request rejected with 400"

rm -rf "$WORK"
log "fdb CLI + HTTP tests passed"
