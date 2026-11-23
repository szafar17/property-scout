# Property Scout — developer tasks
PLIST_LABEL := com.propertyscout.daily
PLIST_SRC   := scripts/$(PLIST_LABEL).plist
PLIST_DST   := $(HOME)/Library/LaunchAgents/$(PLIST_LABEL).plist
PROJECT_DIR := $(shell pwd)

.PHONY: help setup test lint typecheck check run-mock install-schedule uninstall-schedule

help:
	@echo "make setup             - create venv and install deps via uv"
	@echo "make test              - run pytest"
	@echo "make lint              - ruff check"
	@echo "make typecheck         - mypy"
	@echo "make check             - preflight credential check (no Notion writes)"
	@echo "make run-mock          - dry-run the pipeline offline with MockSource"
	@echo "make install-schedule  - install the 06:00 America/New_York launchd job"
	@echo "make uninstall-schedule- remove the launchd job"

setup:
	uv venv
	uv pip install -e ".[dev]"

test:
	uv run python -m pytest -q

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy src

check:
	uv run property-scout --check

run-mock:
	uv run property-scout --sources mock --dry-run --no-enrich

install-schedule:
	mkdir -p $(PROJECT_DIR)/logs
	sed 's|__PROJECT_DIR__|$(PROJECT_DIR)|g' $(PLIST_SRC) > $(PLIST_DST)
	launchctl unload $(PLIST_DST) 2>/dev/null || true
	launchctl load $(PLIST_DST)
	@echo "Loaded $(PLIST_LABEL); runs daily at 06:00 America/New_York."

uninstall-schedule:
	launchctl unload $(PLIST_DST) 2>/dev/null || true
	rm -f $(PLIST_DST)
	@echo "Removed $(PLIST_LABEL)."
