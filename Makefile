# Pramana - standard evaluation interface
#   make setup   install dependencies into ./.venv
#   make run     launch the harness (interactive; or: make run REPO=<path|url> ISSUE=<url|file|text> TEST="<cmd>")
#   make test    unit tests + (if AI_API_KEY is set) a quick end-to-end run on 2 bundled tasks with hidden tests
#   make bench   the full bundled benchmark (4 tasks, Python + JavaScript, bug fixes + a feature)
#   make clean   remove generated artefacts
# The API key is read from the environment (AI_API_KEY) and never stored in the repository.

SHELL   := /bin/bash
VENV    := .venv
BIN     := $(VENV)/bin
PRAMANA := $(BIN)/pramana

.PHONY: setup run test clean doctor bench help

help:
	@sed -n '2,7p' Makefile

setup:
	@bash scripts/setup.sh

run:
	@test -x $(PRAMANA) || { echo "Pramana is not installed yet: run 'make setup' first."; exit 1; }
	@AI_API_KEY="$(AI_API_KEY)" $(PRAMANA) run $(if $(REPO),--repo "$(REPO)") $(if $(ISSUE),--issue "$(ISSUE)") $(if $(TEST),--test "$(TEST)")

test:
	@test -x $(PRAMANA) || { echo "Pramana is not installed yet: run 'make setup' first."; exit 1; }
	@$(BIN)/python -m pytest -q tests
	@if [ -n "$(AI_API_KEY)" ]; then \
		AI_API_KEY="$(AI_API_KEY)" $(PRAMANA) bench --suite quick --plain; \
	else \
		echo "AI_API_KEY is not set: skipped the end-to-end benchmark (unit tests only)."; \
	fi

doctor:
	@AI_API_KEY="$(AI_API_KEY)" $(PRAMANA) doctor

bench:
	@AI_API_KEY="$(AI_API_KEY)" $(PRAMANA) bench --suite $(or $(SUITE),mini) --plain

clean:
	@rm -rf $(VENV) runs workspace .pytest_cache build dist *.egg-info
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "cleaned."
