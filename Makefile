.PHONY: install dev test test-verbose test-coverage lint check clean docs help all build-dist verify-dist-install validate-mcp-registry distribute

PYTHON ?= python3
PROJECT_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
ORIGINAL_PYTHONPATH := $(PYTHONPATH)
LOCAL_PYTHONPATH := $(PROJECT_ROOT)/src$(if $(ORIGINAL_PYTHONPATH),:$(ORIGINAL_PYTHONPATH))
export PYTHONPATH := $(LOCAL_PYTHONPATH)
VERSION := $(shell PYTHONPATH="$(LOCAL_PYTHONPATH)" $(PYTHON) -c "from kindex import __version__; print(__version__)")
DIST_WHEEL := dist/kindex-$(VERSION)-py3-none-any.whl

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

all: install test ## Install and run tests

install: ## Install kindex (kin CLI)
	$(PYTHON) -m pip install -e .

dev: ## Install with dev + LLM dependencies
	$(PYTHON) -m pip install -e ".[dev,llm]"

test: ## Run test suite
	$(PYTHON) -m pytest tests/ -x -q

test-verbose: ## Run tests with full output
	$(PYTHON) -m pytest tests/ -v

test-coverage: ## Run tests with coverage report
	$(PYTHON) -m pytest tests/ --cov=kindex --cov-report=term-missing -q

lint: ## Run basic checks
	$(PYTHON) -m py_compile src/kindex/cli.py
	$(PYTHON) -m py_compile src/kindex/store.py
	$(PYTHON) -m py_compile src/kindex/config.py
	$(PYTHON) -m py_compile src/kindex/extract.py
	$(PYTHON) -m py_compile src/kindex/ingest.py
	$(PYTHON) -m py_compile src/kindex/analytics.py
	$(PYTHON) -m py_compile src/kindex/reminders.py
	$(PYTHON) -m py_compile src/kindex/notify.py

check: lint test ## Lint + test combined

build-dist: ## Build source/wheel distributions
	@$(PYTHON) -c "import build" 2>/dev/null || (echo "Missing build module. Run: $(PYTHON) -m pip install build  (or: make dev)" && exit 1)
	$(PYTHON) -m build

verify-dist-install: build-dist ## Verify built wheel installs with MCP extra
	@test -f "$(DIST_WHEEL)" || (echo "Missing $(DIST_WHEEL)" && exit 1)
	@TMP_DIR=$$(mktemp -d); \
	echo "Installing $(DIST_WHEEL)[mcp] into $$TMP_DIR"; \
	$(PYTHON) -m pip install --quiet --no-cache-dir --target "$$TMP_DIR" "$(DIST_WHEEL)[mcp]"; \
	PYTHONPATH="$$TMP_DIR" $(PYTHON) -S -c "import kindex, kindex.mcp_server; print('verified', kindex.__version__)"

validate-mcp-registry: ## Validate server.json with mcp-publisher (ALLOW_SKIP_REGISTRY=1 to skip when absent)
	@if command -v mcp-publisher >/dev/null 2>&1; then \
		mcp-publisher validate server.json; \
	elif [ "$(ALLOW_SKIP_REGISTRY)" = "1" ]; then \
		echo "SKIPPED MCP Registry validation (ALLOW_SKIP_REGISTRY=1): mcp-publisher is not installed"; \
	else \
		echo "MCP Registry validation did not run: mcp-publisher is not installed" >&2; \
		echo "Install from https://github.com/modelcontextprotocol/registry/releases, or set ALLOW_SKIP_REGISTRY=1" >&2; \
		exit 1; \
	fi

distribute: check verify-dist-install validate-mcp-registry ## Release preflight: tests, build, install, registry metadata validation

clean: ## Remove build artifacts
	rm -rf build/ dist/ *.egg-info src/*.egg-info .pytest_cache .coverage htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

docs: ## Open landing page in browser
	open docs/index.html 2>/dev/null || xdg-open docs/index.html 2>/dev/null || echo "Open docs/index.html in your browser"

version: ## Show current version
	@$(PYTHON) -c "from kindex import __version__; print(__version__)"

ship: distribute ## Factory endgame gate: full check + dist verify + registry validation
	@echo "ship gate green"

test-isolation: build-dist ## Factory endgame gate: wheel + CLI + stdin-closed import in a bare venv
	@set -e; TMP=$$(mktemp -d); \
	python3 -m venv "$$TMP/venv"; \
	"$$TMP/venv/bin/pip" install --quiet --no-cache-dir "$(DIST_WHEEL)"; \
	"$$TMP/venv/bin/kin" --version >/dev/null; \
	"$$TMP/venv/bin/python" -c "import kindex.config" </dev/null; \
	python3 -m venv "$$TMP/venv-all"; \
	"$$TMP/venv-all/bin/pip" install --quiet --no-cache-dir "$(DIST_WHEEL)[all]"; \
	"$$TMP/venv-all/bin/python" -c "import kindex.mcp_server" </dev/null; \
	echo "isolation gate green: wheel + CLI + stdin-safe config; [all] extra resolves to a runnable MCP server"
