#!/bin/sh
# If an extra CA was staged (.local/extra-ca.pem, mounted at /run/fdb/extra-ca.pem), trust it for
# git clones of source repos, alongside the system bundle.
set -e
if [ -s /run/fdb/extra-ca.pem ]; then
  cat /etc/ssl/certs/ca-certificates.crt /run/fdb/extra-ca.pem > /tmp/fdb-ca.pem
  export GIT_SSL_CAINFO=/tmp/fdb-ca.pem SSL_CERT_FILE=/tmp/fdb-ca.pem
fi
exec "$@"
