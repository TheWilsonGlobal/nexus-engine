# Nexus Engine SDK Makefile
# Cross-platform: every recipe is a single command.
# Override the interpreter with: make test PYTHON=python3
-include .env
export

PYTHON ?= python
HOST ?= $(if $(BACKEND_HOST),$(BACKEND_HOST),127.0.0.1)
PORT ?= $(if $(BACKEND_PORT),$(BACKEND_PORT),8766)

.PHONY: install lint test run dev

# Dev install (editable mode)
install:
	$(PYTHON) -m pip install -e ".[dev,pdf,observability]"

# Code linting
lint:
	$(PYTHON) -m ruff check nexus_engine tests

# Run SDK contract tests
test:
	$(PYTHON) -m pytest tests

# Run standalone FastAPI server
run:
	$(PYTHON) -m uvicorn nexus_engine.main:create_app --factory --host $(HOST) --port $(PORT)

# Run server with auto-reload for SDK development
dev:
	$(PYTHON) -m uvicorn nexus_engine.main:create_app --factory --host $(HOST) --port $(PORT) --reload --reload-dir nexus_engine
