#!/usr/bin/env bash
# Creates .venv with uv on first run, then runs pytest. Delete .venv to rebuild it.
#
# Everything the suite needs is pinned here, so a fresh .venv is the environment the tests were
# written against and not whatever the resolver falls back to (#187):
# - Python >= 3.14.2: Home Assistant 2026.9 requires it. Asking uv for plain "3.14" can pick a
#   cached 3.14.0, and the resolver then quietly installs an older Home Assistant.
# - pytest-homeassistant-custom-component is pinned, and each release pins one Home Assistant.
# - The conversation component's own requirements (hassil, home-assistant-intents,
#   gazetteer-matcher, …) are read from its manifest in the installed Home Assistant, so they
#   always match the version Home Assistant itself pins.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_SPEC=">=3.14.2"
PHCC_VERSION="0.13.366"   # pins homeassistant==2026.9.3
HA_VERSION="2026.9.3"
# Components the tests set up whose requirements Home Assistant would install at runtime.
COMPONENTS="conversation"

UV="${UV:-}"
for candidate in "$UV" "$HOME/.hermes/bin/uv" "$HOME/.local/bin/uv"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then UV="$candidate"; break; fi
done
if [ -z "$UV" ] || [ ! -x "$UV" ]; then UV=uv; fi

if [ ! -x .venv/bin/python ]; then
  "$UV" venv --python "$PYTHON_SPEC" .venv
  "$UV" pip install --python .venv/bin/python "pytest-homeassistant-custom-component==$PHCC_VERSION"
  # shellcheck disable=SC2086
  requirements=$(.venv/bin/python - $COMPONENTS <<'PY'
import json, pathlib, sys
import homeassistant.components as components

root = pathlib.Path(components.__file__).parent
for name in sys.argv[1:]:
    for requirement in json.loads((root / name / "manifest.json").read_text()).get("requirements", []):
        print(requirement)
PY
)
  if [ -n "$requirements" ]; then
    # shellcheck disable=SC2086
    "$UV" pip install --python .venv/bin/python $requirements
  fi
fi

# Fail fast on an environment that is not the tested one, e.g. a .venv built by an older script.
installed=$(.venv/bin/python -c "from homeassistant.const import __version__; print(__version__)")
if [ "$installed" != "$HA_VERSION" ]; then
  echo "test.sh: .venv has Home Assistant $installed, expected $HA_VERSION. Delete .venv and run this again." >&2
  exit 1
fi

exec .venv/bin/python -m pytest "$@"
