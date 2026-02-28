.PHONY: install install-dev lint format test test-cov backtest paper live setup-wallet find-markets clean help

PYTHON := python
PIP := pip
BOT := $(PYTHON) main.py

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Installation ─────────────────────────────────────────────────────────────
install:  ## Install production dependencies
	$(PIP) install -r requirements.txt

install-dev:  ## Install all dependencies including dev tools
	$(PIP) install -r requirements-dev.txt
	pre-commit install
	pre-commit run detect-secrets --all-files || true
	@echo "✓ Dev environment ready"

# ── Code Quality ─────────────────────────────────────────────────────────────
lint:  ## Run ruff linter
	ruff check .

format:  ## Auto-format code with ruff + black
	ruff format .
	black .

format-check:  ## Check formatting without modifying files
	ruff format --check .
	black --check .

# ── Testing ───────────────────────────────────────────────────────────────────
test:  ## Run unit tests (fast, no network)
	pytest -m "not integration and not wallet and not slow"

test-all:  ## Run all tests including slow/integration
	pytest

test-cov:  ## Run tests with coverage report
	pytest --cov=. --cov-report=term-missing --cov-report=html -m "not integration and not wallet"
	@echo "Coverage report: htmlcov/index.html"

# ── Bot Modes ─────────────────────────────────────────────────────────────────
backtest:  ## Run walk-forward backtest
	$(BOT) --mode backtest

paper:  ## Run in paper trading mode (no real orders)
	$(BOT) --mode paper

live:  ## Run in live trading mode (REAL MONEY — requires confirmation)
	@echo "⚠️  WARNING: Live mode uses REAL funds on Polygon mainnet."
	@echo "   Ensure PAPER_TRADING=false in .env and you have reviewed all risks."
	@read -p "Type 'CONFIRM LIVE TRADING' to proceed: " confirm; \
	  if [ "$$confirm" = "CONFIRM LIVE TRADING" ]; then \
	    $(BOT) --mode live; \
	  else \
	    echo "Aborted."; \
	  fi

tui:  ## Launch Textual TUI dashboard
	$(PYTHON) -m control.tui

# ── Setup Scripts ─────────────────────────────────────────────────────────────
setup-wallet:  ## Interactive wallet + contract approval setup
	$(PYTHON) scripts/setup_wallet.py

find-markets:  ## Discover current BTC 5-min markets and save token IDs
	$(PYTHON) scripts/find_btc_markets.py

# ── Maintenance ───────────────────────────────────────────────────────────────
clean:  ## Remove build artifacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage dist build *.egg-info
	@echo "✓ Cleaned"

secrets-baseline:  ## Regenerate detect-secrets baseline (run after adding new files)
	detect-secrets scan --baseline .secrets.baseline
