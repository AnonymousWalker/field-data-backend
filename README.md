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
| `fdb-api` | `tools/fdb` | Local HTTP helper for import/put/status (see *Working with real repos*). |
| `tools` (profile) | `azure-cli` | One-off Azure CLI used by the seed, e.g. `docker compose run --rm tools az storage blob list -c '$web'`. |

## Working with real repos (`fdb`)

`fdb` lets you test the pipeline with real content in three steps: **import** a repo, **edit** a file, and **check** that
the change reached languageapi. You can drive it from the command line (`tools/cli/fdb`) or over HTTP
(`http://localhost:7080`). Both do the same thing.

Everything stays on your machine. fdb only pushes to the local WACS, and it can never push back to the repo you imported from.

Before you start, run `make up` (and `make seed` on a fresh stack).

### From the command line

**1. Import a repo.** This copies it into the local WACS and waits until it has been rendered:

```bash
tools/cli/fdb import https://content.bibletranslationtools.org/anonymouswalker/en_ulb --wait
```

**2. Edit a file.** Download it, change it, and send it back. Each `put` makes one commit, just like a real push:

```bash
curl -o jud.usfm http://localhost:3000/anonymouswalker/en_ulb/raw/branch/master/66-JUD.usfm
# edit jud.usfm in any editor
tools/cli/fdb put anonymouswalker/en_ulb 66-JUD.usfm jud.usfm -m "Fix Jude 1:1" --wait
```

**3. Check the result.** `--wait` already did this, but you can ask again at any time:

```bash
tools/cli/fdb status anonymouswalker/en_ulb
```

```
anonymouswalker/en_ulb @ 9a3b13d04e: ok (0.7s)
  wacs         ok   commit=9a3b13d04e…, message=Fix Jude 1:1
  render       ok   ended_at=…, converter=Bible.Normal, output=http://localhost:10000/…/index.json
  languageapi  ok   rendered_files=1258, updated_at=…
```

All three lines say `ok` when the change has gone all the way through:

- **wacs:** the commit is in the local WACS.
- **render:** SRP rendered it.
- **languageapi:** the public API has the new data.

`pending` means it's still working. `failed` comes with the reason.

Other commands:

- `tools/cli/fdb list`: show every local repo and whether it has been rendered.
- `tools/cli/fdb --help`, or `tools/cli/fdb <command> --help`: all options.
- `--json` (before the command, e.g. `tools/cli/fdb --json status …`): machine-readable output for scripts.

Prefer `make`? These do the same:

```bash
make import URL=https://content.bibletranslationtools.org/anonymouswalker/en_ulb WAIT=300
make put REPO=anonymouswalker/en_ulb FILE_PATH=66-JUD.usfm SRC=jud.usfm MSG="Fix Jude 1:1" WAIT=300
make status REPO=anonymouswalker/en_ulb
make repos
```

### Over HTTP

The same three steps with `curl`, or from Postman or any script. Every response is JSON.

**1. Import a repo:**

```bash
curl -X POST http://localhost:7080/import \
  -H 'content-type: application/json' \
  -d '{"url": "https://content.bibletranslationtools.org/anonymouswalker/en_ulb", "wait": 300}'
```

**2. Edit a file.** The request body is the new file content:

```bash
curl -X PUT 'http://localhost:7080/repos/anonymouswalker/en_ulb/contents/66-JUD.usfm?message=Fix%20Jude%201:1&wait=300' \
  --data-binary @jud.usfm
```

**3. Check the result:**

```bash
curl http://localhost:7080/repos/anonymouswalker/en_ulb/pipeline
```

The pipeline check returns `200` when everything is done, `202` while it's still working, and `409` if a step failed.

All endpoints:

| What | Request |
|---|---|
| Import a repo | `POST /import` with `{"url": "...", "as": "owner/repo", "wait": 300}` (`as` and `wait` are optional) |
| Replace a file | `PUT /repos/{owner}/{repo}/contents/{path}?message=...&wait=300`, with the file as the body |
| Check a commit | `GET /repos/{owner}/{repo}/pipeline?commit=...&wait=300` (defaults to the latest commit) |
| List repos | `GET /repos` |
| Health | `GET /healthz` |

### Good to know

- **`wait` is in seconds.** Without it, a command returns right away, and you check later with `status` or `/pipeline`.
- **Rename on import** with `--as owner/repo` (CLI) or `"as"` (HTTP). This lets you import the same repo twice, e.g. `--as qa/en_ulb`.
- **The HTTP helper runs inside Docker,** so it can't read files by path on your machine. Send file contents in the request body instead. It can also import from the local WACS itself: `"url": "http://wacs:3000/owner/repo.git"`.
- **Separate copies:** the CLI and the HTTP helper each keep their own copy of imported repos. `make nuke` deletes both.
- **Exit codes** (CLI): `0` all ok, `1` a step failed, `2` bad input, `3` still pending when `--wait` ran out.
- **Self-test:** `make test-tools` runs an offline test of both the CLI and the HTTP helper.

## System design diagram

New here? Start with **[`docs/architecture/`](docs/architecture/)**. It's a [GitDiagram](https://gitdiagram.com)-style system design diagram of the whole stack: one box per system, the containers, Azure Functions, Service Bus topics and data stores inside them, and labelled arrows showing what flows where. An arrow out of a topic names the subscription and its filter.

- **On GitHub:** [`docs/architecture/README.md`](docs/architecture/README.md) shows the diagram directly, followed by the hubs, the links between systems, and known gaps.
- **In a browser:** open `docs/architecture/index.html` (`xdg-open docs/architecture/index.html`, or `open` on macOS). You can pan and zoom, hover a box for a summary, and click it to open its source at the pinned commit. The **walkthroughs** below the diagram (*a commit's journey*, *bringing the stack up*, *testing with fdb*) highlight each step. You can also copy the Mermaid source or download an SVG. The page loads Mermaid from a CDN, so it needs internet access.

The diagram is **generated from the code**, so it stays accurate. `tools/graph/build.py` reads the compose files, the Service Bus config, SRP's and languageapi's function definitions, the Drizzle schema, the Hasura metadata, the fdb package and the Makefile into a full graph ([`graph.json`](docs/architecture/graph.json), about 150 items). `tools/graph/mermaid.py` then draws it at component level, folding tables into their database, subscriptions into arrows, and look-alike functions into one box. `tools/graph/knowledge.json` holds what the code can't say: descriptions, a few runtime-only links (like the WACS → SRP webhook), the walkthroughs, and how the diagram groups things. Every item it names must exist, so a rename fails the build instead of leaving stale docs.

```bash
make graph         # regenerate docs/architecture/ (needs PyYAML and checked-out submodules)
```

```bash
make graph-check   # fail if docs/architecture/ is out of date with the code
```

Run `make graph` and commit the result whenever you change compose files, config, scripts, the Makefile or the submodule pins. To reword a box or a walkthrough, or change how things are grouped, edit `tools/graph/knowledge.json`; to change the page itself, edit `tools/graph/viewer.html`.

## Layout

```
compose.yaml                 root compose file; `include:`s the files below
compose/{infra,wacs,languageapi,srp}.yaml
compose/overrides/           optional overlays (wacs-prebuilt.yaml)
docker/                      umbrella-owned Dockerfiles (build context = the submodule)
docs/architecture/           generated system design diagram: README.md (renders on GitHub), index.html, diagram.mmd, graph.json
config/                      emulator config + env overrides for upstream config
seed/                        seed.sh, fixtures (demo Bible repo), templates, reference data, SQL
scripts/                     smoke.sh and shared helpers (lib.sh)
tools/                       fdb support tools:
  fdb/                       shared Python package (stdlib only): core, clients, CLI and HTTP front ends
  cli/fdb                    CLI entry point
  http/                      fdb-api image (Dockerfile, entrypoint)
  test.sh                    offline end-to-end test of both (make test-tools)
  graph/                     diagram generator: build.py (extract), mermaid.py (draw), knowledge.json, viewer.html
repos/                       submodules: wacs (prod), srp (master), languageapi (prod)
```

## Updating the pinned repos

The submodules are pinned to exact commits, so everyone tests the same code.

1. Run `make bump` to move each one to the tip of its tracked branch.
2. Run `make reset` to check the new pins still work together.
3. Run `make graph` so the system design diagram matches the new code.
4. Commit the new pins and `docs/architecture/`.

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
