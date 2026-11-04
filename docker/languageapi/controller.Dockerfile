# syntax=docker/dockerfile:1.7
# Umbrella-owned build for repos/languageapi/controller (Azure Functions v4, Node).
# Build context: repos/languageapi/controller. Targets:
#   migrate  - runs the Drizzle migrations once against $DATABASE_URL, then exits
#   runtime  - the Functions host serving the controller (HTTP routes, Service Bus + timer triggers)
#
# Differences from the upstream controller/Dockerfile: Node 22 (matches CI), pnpm from packageManager
# (10.x, upstream pins 8.x), no global func core tools (the base image already hosts functions),
# multi-stage so the runtime image doesn't carry devDependencies.
#
# Optional build secret `extra_ca` (PEM bundle) is trusted during dependency installs, for networks
# with a TLS-intercepting proxy. It is never written into an image layer.

ARG NODE_VERSION=22

FROM node:${NODE_VERSION}-bookworm-slim AS build
WORKDIR /app
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    corepack enable && pnpm install --frozen-lockfile
COPY . .
RUN pnpm run build

FROM build AS prod-deps
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    pnpm prune --prod

# One-shot schema migration (drizzle/ SQL files are read relative to the working directory).
FROM node:${NODE_VERSION}-bookworm-slim AS migrate
WORKDIR /app
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY --from=build /app/drizzle ./drizzle
CMD ["node", "dist/src/migrate.js"]

FROM mcr.microsoft.com/azure-functions/node:4-node${NODE_VERSION} AS runtime
ENV AzureWebJobsScriptRoot=/home/site/wwwroot \
    AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    FUNCTIONS_WORKER_RUNTIME=node
WORKDIR /home/site/wwwroot
COPY --from=prod-deps /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY --from=build /app/package.json /app/host.json ./
