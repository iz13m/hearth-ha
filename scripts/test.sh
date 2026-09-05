#!/usr/bin/env bash
# Creates .venv with uv (Python 3.13) on first run, then runs pytest.
set -euo pipefail
cd "$(dirname "$0")/.."
UV="${UV:-$HOME/.hermes/bin/uv}"
[ -x "$UV" ] || UV=uv
if [ ! -x .venv/bin/python ]; then
  "$UV" venv --python 3.14 .venv
  "$UV" pip install --python .venv/bin/python pytest-homeassistant-custom-component hassil home-assistant-intents
fi
exec .venv/bin/python -m pytest "$@"
