#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export QT_QPA_PLATFORM=offscreen
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export IBKR_BOT_HEADLESS_SIGNALS=1

python3 -m compileall -q app tests scripts main.py
python3 -m coverage erase
python3 -X utf8 -W error::ResourceWarning -m coverage run --branch --source=app,main -m pytest -q --tb=short -ra --disable-warnings -m "not soak"
python3 -m coverage report --show-missing --fail-under=75
python3 -m coverage json -o coverage.json
python3 -m coverage xml -o coverage.xml
python3 scripts/check_callable_coverage.py --coverage-json coverage.json --source app --source main.py
python3 -X utf8 -W error::ResourceWarning -m pytest -q --tb=short -ra --disable-warnings -m soak
python3 scripts/run_mutation_smoke.py
python3 scripts/run_all_simulations.py
