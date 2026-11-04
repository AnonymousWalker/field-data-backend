# field-data-backend: one-command local stack for WACS + ScriptureRenderingPipeline + languageapi.
# Run `make help` for targets. Settings come from .env (see .env.example) or the environment.

-include .env
export

SHELL := /bin/bash
COMPOSE_FILE_ARGS := -f compose.yaml
ifeq ($(WACS_PREBUILT),1)
COMPOSE_FILE_ARGS += -f compose/overrides/wacs-prebuilt.yaml
endif
export COMPOSE_FILE_ARGS
COMPOSE := docker compose $(COMPOSE_FILE_ARGS)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: init
init: ## Check out the pinned submodule commits
	git submodule update --init --recursive

.PHONY: prepare
prepare:
	@test -f repos/wacs/.env && test -d repos/srp/PipelineCommon && test -d repos/languageapi/controller \
		|| { echo "Submodules missing: run 'make init' first" >&2; exit 1; }
	@mkdir -p .local
	@if [ -n "$(EXTRA_CA_FILE)" ]; then cp "$(EXTRA_CA_FILE)" .local/extra-ca.pem; else : > .local/extra-ca.pem; fi

.PHONY: build
build: prepare ## Build all locally-built images
	$(COMPOSE) build

.PHONY: up
up: prepare ## Build (if needed) and start everything, waiting until healthy
	$(COMPOSE) up -d --build --wait

.PHONY: down
down: ## Stop containers (keeps data volumes)
	$(COMPOSE) --profile tools down --remove-orphans

.PHONY: nuke
nuke: ## Stop containers and delete all data volumes
	$(COMPOSE) --profile tools down -v --remove-orphans

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps -a

.PHONY: logs
logs: ## Follow logs (S=service to filter, e.g. make logs S=srp-worker)
	$(COMPOSE) logs -f --tail 100 $(S)

.PHONY: seed
seed: ## Seed admin user, webhook, storage containers, reference data and demo repo
	./seed/seed.sh

.PHONY: smoke
smoke: ## Health checks + end-to-end render flow
	./scripts/smoke.sh

.PHONY: import
import: ## Import a git repo into the local WACS (URL=<git url> [AS=owner/repo] [WAIT=300])
	@test -n "$(URL)" || { echo "usage: make import URL=<git url> [AS=owner/repo] [WAIT=300]" >&2; exit 2; }
	./tools/cli/fdb import "$(URL)" $(if $(AS),--as "$(AS)") $(if $(WAIT),--wait $(WAIT))

.PHONY: put
put: ## Replace one file in one commit (REPO=owner/repo FILE_PATH=<path in repo> SRC=<local file> [MSG=] [WAIT=])
	@test -n "$(REPO)" -a -n "$(FILE_PATH)" -a -n "$(SRC)" || { echo "usage: make put REPO=o/r FILE_PATH=<path> SRC=<file> [MSG=..] [WAIT=300]" >&2; exit 2; }
	./tools/cli/fdb put "$(REPO)" "$(FILE_PATH)" "$(SRC)" $(if $(MSG),-m "$(MSG)") $(if $(WAIT),--wait $(WAIT))

.PHONY: status
status: ## Pipeline status of a repo's head commit (REPO=owner/repo [WAIT=300])
	@test -n "$(REPO)" || { echo "usage: make status REPO=owner/repo [WAIT=300]" >&2; exit 2; }
	./tools/cli/fdb status "$(REPO)" $(if $(WAIT),--wait $(WAIT))

.PHONY: repos
repos: ## List local WACS repos and their pipeline state
	./tools/cli/fdb list

.PHONY: test-tools
test-tools: ## Offline end-to-end test of the fdb CLI and HTTP API
	./tools/test.sh

.PHONY: graph
graph: ## Regenerate the system design diagram (docs/architecture/)
	python3 tools/graph/build.py

.PHONY: graph-check
graph-check: ## Fail if docs/architecture/ is out of date with the code
	python3 tools/graph/build.py --check

.PHONY: reset
reset: nuke up seed smoke ## Fresh stack from scratch: nuke, up, seed, smoke

.PHONY: bump
bump: ## Move submodules to the tip of their tracked branches (review, then commit)
	git submodule update --remote
	@git submodule status
	@echo "Review with 'git diff --submodule', then commit the new pins."
