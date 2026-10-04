# Climate AI: everyday commands. `make` (or `make help`) lists them.
# Works with the GNU make 3.81 that macOS ships. Everything runs `docker compose` from this
# directory, so .env (COMPOSE_FILE, COMPOSE_PROFILES, ports) applies exactly as it does by hand.

SHELL := /bin/bash
COMPOSE ?= docker compose
# make logs SVC=worker, make restart SVC=app
SVC ?=
# extra arguments for bootstrap / reset / update (e.g. ARGS=--lan) and test (pytest arguments)
ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help bootstrap up down restart ps logs doctor password token tokens shell psql reset \
        update backup test homekit-native dev web-dev typecheck-web

help: ## list the commands
	@printf 'Climate AI\n\n'
	@grep -E '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-15s %s\n", $$1, $$2}'
	@printf '\nFirst time: make bootstrap   (docs/LOCAL_DEVELOPMENT.md, docs/DEPLOY.md)\n'

bootstrap: ## first run, and safe to repeat: .env + secrets, build, start, wait, print the URL (ARGS=--lan|--homekit)
	@./scripts/bootstrap.sh $(ARGS)

up: ## build and start (or update) the stack
	$(COMPOSE) up -d --build

down: ## stop the stack (all data is kept)
	$(COMPOSE) down

restart: ## restart the services (SVC=app for one)
	$(COMPOSE) restart $(SVC)

ps: ## container status
	$(COMPOSE) ps

logs: ## follow the logs (SVC=worker for one service)
	$(COMPOSE) logs -f --tail=200 $(SVC)

doctor: ## check the installation: secrets, database, owner password, heartbeats, data source
	$(COMPOSE) exec app python -m climate.cli doctor

password: ## set the owner password from this computer (break-glass; signs out every device)
	$(COMPOSE) exec app python -m climate.cli set-password

token: ## create an API token, shown once: make token NAME=dashboard [ROLE=viewer|control|agent] [DAYS=90] [REMOTE=1]
	@if [ -z "$(NAME)" ]; then echo 'usage: make token NAME=<what uses it> [ROLE=agent|viewer|control] [DAYS=<expiry>] [REMOTE=1]'; exit 2; fi
	$(COMPOSE) exec app python -m climate.cli create-token --name "$(NAME)" $(if $(ROLE),--role $(ROLE)) $(if $(DAYS),--expires-days $(DAYS)) $(if $(REMOTE),--allow-remote)

tokens: ## list API tokens (names, roles, last use; never the secrets)
	$(COMPOSE) exec app python -m climate.cli list-tokens

shell: ## a shell in the app container
	$(COMPOSE) exec app bash

psql: ## psql on the Climate AI database
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

reset: ## DELETE all data (history, settings, owner password, sign-ins, HomeKit pairings), keep .env, start fresh
	@if [ "$(YES)" != "1" ]; then \
	  printf '%s\n' 'This deletes the database and caches: history, settings, the owner password, the ecobee' \
	    'sign-in and the HomeKit pairings (each thermostat then needs a HomeKit reset before it can' \
	    'pair again). .env and its secrets are kept.'; \
	  printf 'Type "reset" to continue: '; read -r answer; \
	  if [ "$$answer" != "reset" ]; then echo 'Nothing deleted.'; exit 1; fi; \
	fi
	$(COMPOSE) down -v --remove-orphans
	@./scripts/bootstrap.sh $(ARGS)

update: ## git pull, then bootstrap (adds new secrets to .env, rebuilds, waits for health)
	git pull --ff-only
	@./scripts/bootstrap.sh $(ARGS)

backup: ## dump the database to ~/climate-ai-backups (deploy/backup.sh)
	./deploy/backup.sh

test: ## API + agent tests natively against the Docker database (ARGS= pytest arguments)
	@./scripts/test.sh $(ARGS)

homekit-native: ## macOS: run the HomeKit service on this computer (Docker Desktop cannot do mDNS)
	@./scripts/homekit-native.sh

dev: ## development: API with live reload (docker-compose.dev.yml) + Vite hot reload on :5173
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up -d --build
	@./scripts/web-dev.sh

web-dev: ## only the Vite dev server (the dev API is already up)
	@./scripts/web-dev.sh

typecheck-web: ## type-check the web app (vue-tsc --noEmit; never emits files)
	cd web && { [ -d node_modules ] || npm ci; } && npm run typecheck
