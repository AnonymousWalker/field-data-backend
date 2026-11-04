# syntax=docker/dockerfile:1.7
# fdb-api: the fdb HTTP helper. The code itself (tools/fdb) is bind-mounted, so edits apply on restart.
# Optional build secret `extra_ca` is trusted while installing packages (TLS-intercepting proxies);
# it is removed again before the layer is committed.
FROM python:3.13-slim
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then \
      cp /run/secrets/extra_ca /usr/local/share/ca-certificates/fdb-build-extra.crt && update-ca-certificates; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*; \
    rm -f /usr/local/share/ca-certificates/fdb-build-extra.crt && update-ca-certificates --fresh >/dev/null
COPY entrypoint.sh /usr/local/bin/fdb-entrypoint
WORKDIR /app/tools
ENTRYPOINT ["fdb-entrypoint"]
CMD ["python", "-m", "fdb.server"]
