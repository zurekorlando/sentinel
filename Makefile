# ============================================================
#  Sentinel Backup System — Developer convenience targets
#  Usage:  make <target>
#  Requires: Python 3.11+, Node 20+, pip, npm
# ============================================================

.PHONY: help install install-py install-web \
        seed api web dev \
        test test-api test-dpe \
        docker-up docker-down docker-logs \
        clean clean-dev

# ── Pass-through env vars ──────────────────────────────────
SENTINEL_PASSPHRASE ?= demo-passphrase-change-me
SENTINEL_JOBS_DIR   ?= jobs
API_PORT            ?= 8000
WEB_PORT            ?= 5173

# ── Default target ─────────────────────────────────────────
help:
	@echo ""
	@echo "  Sentinel — available targets"
	@echo "  ─────────────────────────────────────────────────"
	@echo "  install       Install Python + Node dependencies"
	@echo "  seed          Create test data + run demo backups"
	@echo "  api           Start FastAPI server (port $(API_PORT))"
	@echo "  web           Start Vite dev server (port $(WEB_PORT))"
	@echo "  dev           seed + api + web  (requires tmux/screen)"
	@echo ""
	@echo "  test          Run all tests"
	@echo "  test-api      Run API smoke tests only"
	@echo "  test-dpe      Run DPE unit tests only"
	@echo ""
	@echo "  docker-up     docker compose up -d"
	@echo "  docker-down   docker compose down"
	@echo "  docker-logs   Follow all container logs"
	@echo ""
	@echo "  clean         Remove build artefacts"
	@echo "  clean-dev     Remove dev catalog + store + test data"
	@echo ""

# ── Install ────────────────────────────────────────────────
install: install-py install-web
	@echo "\n✅  All dependencies installed."

install-py:
	pip install -r requirements.txt

install-web:
	cd web && npm install

# ── Demo data ──────────────────────────────────────────────
seed:
	SENTINEL_PASSPHRASE=$(SENTINEL_PASSPHRASE) python scripts/seed_demo.py

# ── API server ─────────────────────────────────────────────
api:
	SENTINEL_PASSPHRASE=$(SENTINEL_PASSPHRASE) \
	SENTINEL_JOBS_DIR=$(SENTINEL_JOBS_DIR) \
	uvicorn api.main:app --reload --port $(API_PORT)

# ── Web dev server ─────────────────────────────────────────
web:
	cd web && npm run dev -- --port $(WEB_PORT)

# ── Combined dev mode (opens two background processes) ─────
# Note: works best on Linux/macOS. On Windows use separate terminals.
dev: seed
	@echo "\nStarting API in background…"
	SENTINEL_PASSPHRASE=$(SENTINEL_PASSPHRASE) \
	SENTINEL_JOBS_DIR=$(SENTINEL_JOBS_DIR) \
	uvicorn api.main:app --reload --port $(API_PORT) &
	@echo "Starting Vite in background…"
	cd web && npm run dev -- --port $(WEB_PORT)

# ── Tests ──────────────────────────────────────────────────
test:
	pytest tests/ -v --tb=short

test-api:
	pytest tests/test_api.py -v --tb=short

test-dpe:
	pytest tests/test_dpe.py -v --tb=short

# ── Docker ─────────────────────────────────────────────────
docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

# ── Cleanup ────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf web/dist web/node_modules/.vite

clean-dev:
	rm -rf test_data/
	rm -f sentinel_catalog_dev.db sentinel_catalog_dev.db-wal sentinel_catalog_dev.db-shm
	rm -f sentinel_catalog_dev.db.cbt.json
	rm -rf sentinel_store_dev/
	@echo "✓ Dev artifacts removed."
