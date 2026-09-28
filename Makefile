# Pramana - standard evaluation interface
#   make setup   install dependencies into ./.venv
#   make run     open Pramana Studio, the app (headless: app served + terminal session; scripted: make run REPO=<path|url> ISSUE=<url|file|text>)
#   make tui     the terminal version
#   make test    unit tests + (if AI_API_KEY is set) a quick end-to-end run on 2 bundled tasks with hidden tests
#   make bench   the full bundled benchmark (4 tasks, Python + JavaScript, bug fixes + a feature)
#   make clean   remove generated artefacts
#   make dmg     build the Mac app + disk image (dist/Pramana-<version>-macos-<arch>.dmg)
# The API key is read from the environment (AI_API_KEY) and never stored in the repository.

SHELL   := /bin/bash
VENV    := .venv
BIN     := $(VENV)/bin
PRAMANA := $(BIN)/pramana
# the key reaches commands through the environment, never through a command line (visible in `ps`)
export AI_API_KEY

.PHONY: stop tui setup run test clean doctor bench help dmg

help:
	@sed -n '2,7p' Makefile

setup:
	@bash scripts/setup.sh

run:
	@test -x $(PRAMANA) || { echo "Pramana is not installed yet: run 'make setup' first."; exit 1; }
	@if [ -n "$(REPO)$(ISSUE)" ]; then \
		$(PRAMANA) run $(if $(REPO),--repo "$(REPO)") $(if $(ISSUE),--issue "$(ISSUE)") $(if $(TEST),--test "$(TEST)"); \
	else \
		$(PRAMANA) ui; \
	fi

stop:
	@curl -s -m 5 -X POST http://127.0.0.1:8765/api/stop-all 2>/dev/null && echo || true
	@pkill -f "pramana (solve|run|bench)" 2>/dev/null; echo "stopped everything Pramana was running"

tui:
	@test -x $(PRAMANA) || { echo "Pramana is not installed yet: run 'make setup' first."; exit 1; }
	@$(PRAMANA) run $(if $(REPO),--repo "$(REPO)") $(if $(ISSUE),--issue "$(ISSUE)") $(if $(TEST),--test "$(TEST)")

test:
	@test -x $(PRAMANA) || { echo "Pramana is not installed yet: run 'make setup' first."; exit 1; }
	@$(BIN)/python -m pytest -q tests
	@if [ -n "$$AI_API_KEY" ]; then \
		$(PRAMANA) bench --suite quick --plain; \
	else \
		echo "AI_API_KEY is not set: skipped the end-to-end benchmark (unit tests only)."; \
	fi

doctor:
	@$(PRAMANA) doctor

bench:
	@$(PRAMANA) bench --suite $(or $(SUITE),mini) --plain

dmg:
	@bash packaging/macos/build_dmg.sh

clean:
	@rm -rf $(VENV) runs workspace .pytest_cache build dist *.egg-info
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "cleaned."
