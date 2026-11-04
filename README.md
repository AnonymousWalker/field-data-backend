# field-data-backend

A local, disposable copy of the WA content pipeline. One command brings up **WACS**, **ScriptureRenderingPipeline (SRP)** and **languageapi**, wired together the way they are in production, and one command tears it all down. It's meant for integration testing and demos/onboarding, on Linux with Docker.

The three projects are pinned as git submodules under `repos/`. Everything needed to run them together lives in this repo: compose files, Dockerfiles, emulator config, seed data and scripts. **Nothing in the submodules is modified.**

## Quick start

```bash
git clone --recurse-submodules <this repo> && cd field-data-backend   # or: make init
make reset     # nuke → build & start → seed → smoke test (first run builds images: ~5–10 min)
```

Then open:

| What | URL |
|---|---|
| WACS (Gitea), login `fdbadmin` / `fdbadmin123` | http://localhost:3000/fielddata/en_ulb |
| Rendered output for the demo repo | http://localhost:10000/devstoreaccount1/$web/u/fielddata/en_ulb/JUD/1.html |
| Hasura console (languageapi GraphQL) | http://localhost:8080/console |
| languageapi controller REST | http://localhost:7072/api/language |

Run `make help` for every target. The common ones:

- `make up` / `make down`: start, or stop while keeping data.
- `make nuke`: stop everything and delete all volumes.
- `make seed`: idempotent seeding, safe to re-run.
- `make smoke`: health checks plus the end-to-end flow.
- `make logs S=srp-worker`: follow one service's logs.
- `make ps`: show service status.

## What runs

This is the flow being reproduced:

```
 push to WACS ──webhook──▶ srp-webhook ──WACSEvent──▶ srp-worker ──renders──▶ Azurite $web
  (Gitea)                   (Functions)   (Service Bus)  (Functions)     │
                                                                       RepoRendered
                                                                         ▼
                                     Hasura ◀── Postgres ◀── lapi-controller (Functions)
```

| Service | From | Notes |
|---|---|---|
| `wacs`, `wacs-db`, `wacs-redis` | `repos/wacs` | Branded Gitea 1.23 + MariaDB + Valkey. Config is upstream `.env` with `config/wacs.env` layered on top. |
| `srp-webhook`, `srp-worker` | `repos/srp` | .NET 10 isolated Functions, built by `docker/srp/functions.Dockerfile`. |
| `lapi-postgres`, `lapi-migrate`, `lapi-hasura`, `lapi-controller` | `repos/languageapi` | Drizzle migrations run once before Hasura and the controller start. |
| `azurite` | Microsoft image | Blob, queue and table storage; also `AzureWebJobsStorage` for every Functions app. |
| `servicebus`, `sb-sql`, `sb-health` | Microsoft images | Service Bus emulator. Topics, subscriptions and filter rules are in `config/servicebus/Config.json`, translated from `repos/srp/infra/main.bicep`. |
| `tools` (profile) | `azure-cli` | One-off Azure CLI used by the seed, e.g. `docker compose run --rm tools az storage blob list -c '$web'`. |

## Layout

```
compose.yaml                 root compose file; `include:`s the files below
compose/{infra,wacs,languageapi,srp}.yaml
compose/overrides/           optional overlays (wacs-prebuilt.yaml)
docker/                      umbrella-owned Dockerfiles (build context = the submodule)
config/                      emulator config + env overrides for upstream config
seed/                        seed.sh, fixtures (demo Bible repo), templates, reference data, SQL
scripts/                     smoke.sh and shared helpers (lib.sh)
repos/                       submodules: wacs (prod), srp (master), languageapi (prod)
```

## Updating the pinned repos

The submodules are pinned to exact commits, so everyone tests the same code.

1. Run `make bump` to move each one to the tip of its tracked branch.
2. Run `make reset` to check the new pins still work together.
3. Commit the new pins.

To test a branch of one repo, check it out inside `repos/<name>` and run `make up`. Images rebuild from whatever is checked out.

## Configuration

Copy `.env.example` to `.env` to change host ports, the demo admin credentials or the demo repo name. Two settings help on restricted networks:

- **`WACS_PREBUILT=1`** uses the published `wycliffeassociates/wacs` image instead of building `repos/wacs`. Use it if the build can't reach the Go module proxy. That image comes from the WACS `prod` CI build, so it may not match the pinned commit exactly.
- **`EXTRA_CA_FILE=/path/to/ca.pem`** trusts that CA during the SRP and languageapi image builds, for networks with a TLS-intercepting proxy. It's passed as a BuildKit secret and is never stored in an image. The upstream WACS Dockerfile can't take it, so use `WACS_PREBUILT=1` there.

## Scope and known gaps

These are not run in v1:

- **BTTWriterCatalog:** needs Cosmos DB change-feed triggers.
- **VerseReportingProcessor:** needs an SQL Server stored procedure that isn't in the repo, plus PORT/Dataverse. It also forces the AMQP-over-WebSockets transport.
- **languageapi's explorer and langnames-sync:** these could come back later as compose profiles.

The Service Bus topics they use are already declared in the emulator config.

Upstream issues this setup works around (worth fixing upstream):

- **`repos/wacs/token-trigger.sql` fails on MariaDB** (`ERROR 1303`, because `CREATE TRIGGER` is inside an `IF`). The seed applies an equivalent idempotent script, `seed/sql/wacs-token-trigger.sql`.
- **The languageapi view `vw_consolidated_repos` isn't created by any migration**, so Hasura flags it, and the "Consolidated Repos" REST endpoint that depends on it, as inconsistent. Everything else works. The smoke test tolerates exactly these two objects.
- **languageapi `controller/Dockerfile` pins pnpm 8** while `package.json` requires pnpm 10. Its compose file also references a `db-migrate` service that doesn't exist. Replaced by `docker/languageapi/controller.Dockerfile`.
- **SRP `ScriptureRenderingPipeline/Dockerfile`** builds Functions Core Tools 4.0.4785 from source, which can't host net10 isolated apps. Replaced by `docker/srp/functions.Dockerfile`.
- **A new repo fires both `create` and `push` webhooks.** Both pass SRP's `OnlyPush` filter, so the repo renders twice. The two `RepoRendered` messages race in the languageapi controller, and one transaction rolls back on the git-row insert (`Failed to create new git row`). The data still lands, but the same race exists in production.

Local-only differences from production:

- **Gitea's `ROOT_URL`** is `http://localhost:3000/`.
- **Webhooks** may target private addresses.
- **CAPTCHA** is off.
- **The `$web` container** is publicly readable, so rendered pages open in a browser.
- **The print template** is a minimal stand-in.
- **The languageapi controller** uses the production subscription names (`NODE_ENV` unset).
