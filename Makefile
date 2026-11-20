# Shortcuts for the commands in AGENTS.md. Each target is one command you can also run by hand.
# Pass pytest arguments through ARGS:  make test ARGS="tests/test_rest_catalog.py -k stale"

UV ?= uv
ARGS ?=
COMPOSE ?= docker compose -f tests/compose/stack.yml

.DEFAULT_GOAL := help
.PHONY: help setup check lint typecheck test test-offline test-live live-up live-down cov package snapshots site

help: ## list the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-13s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## uv, the dev environment and DuckDB's extensions (scripts/setup.py)
	python3 scripts/setup.py

check: lint typecheck test ## what CI's main jobs check

lint: ## ruff check (not ruff format: the code is formatted by hand)
	$(UV) run ruff check .

typecheck: ## pyright, basic mode, over src and scripts
	PYRIGHT_PYTHON_IGNORE_WARNINGS=1 $(UV) run pyright

test: ## the suite: no server, no network
	$(UV) run pytest -q $(ARGS)

test-offline: ## the suite as a machine without DuckDB's extensions runs it
	ICELENS_REQUIRE_DUCKDB_EXTENSIONS=0 ICELENS_TEST_WITHOUT_DUCKDB_EXTENSIONS=1 $(UV) run pytest -q $(ARGS)

test-live: live-up ## the suite against Lakekeeper and Polaris in Docker
	set -a && . ./tests/compose/live.env && set +a && $(UV) run pytest -q $(ARGS)

live-up: ## start Lakekeeper, Polaris and RustFS (tests/compose/stack.yml)
	$(COMPOSE) up -d --wait

live-down: ## stop them and delete everything they stored
	$(COMPOSE) down -v

cov: ## the suite, with line and branch coverage
	$(UV) run pytest -q --cov --cov-report=term-missing:skip-covered $(ARGS)

package: ## build the sdist and wheel, install the wheel in a fresh venv, run it
	ICELENS_TEST_PACKAGING=1 $(UV) run pytest -q tests/test_packaging.py $(ARGS)

snapshots: ## redraw tests/snapshots/ after an intended change to a screen; review the diff
	$(UV) run pytest -q tests/test_snapshots.py --snapshot-update

site: ## the website in _site/: site/, the docs as pages, and the home page's walk-through
	python3 scripts/build_site.py $(ARGS)
