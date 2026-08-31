# Energy Anomaly Watchdog — common tasks. Run from use-cases/energy-watchdog/.

.PHONY: help install data serve demo register-sweeps test lint lint-fix clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

install:  ## Create the venv and install dependencies
	uv sync

data:  ## Generate 90 days of clean interval + weather fixtures for every site
	uv run python data/simulate.py --all --start 2026-06-02 --days 90

serve:  ## Run the real server (Telegram webhook + schedule routes + pipeline)
	uv run python app.py

demo:  ## Trigger one sweep now. Usage: make demo SITE=colombo-plant-1 DATE=2026-08-25
	uv run python demo.py --site $(SITE) --date $(DATE)

register-sweeps:  ## Register the recurring 06:00 Asia/Colombo sweep for every site (needs `make serve` running)
	uv run python scripts/register_sweeps.py

test:  ## Run the test suite
	uv run pytest -q

lint:  ## Check formatting and imports
	uv run black --check . && uv run isort --check-only .

lint-fix:  ## Apply formatting and import sorting
	uv run black . && uv run isort .

clean:  ## Remove generated data, state and caches
	rm -rf state data/interval_*.csv data/weather_*.csv data/faults_*.json .pytest_cache .mypy_cache
