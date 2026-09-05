"""Constants for the Hearth AI integration."""

from __future__ import annotations

import json
from pathlib import Path

DOMAIN = "hearth_ai"
# Read from the manifest (loaded in an executor at import time) so the version we report to the
# hub can never drift from the version HACS installed.
INTEGRATION_VERSION: str = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))["version"]

CONF_HUB_URL = "hub_url"
CONF_PAIRING_TOKEN = "pairing_token"
CONF_INSTALL_SECRET = "install_secret"
CONF_INSTALLATION_ID = "installation_id"
CONF_WS_URL = "ws_url"

DEFAULT_HUB_URL = "https://app.example.com"

# Wire-level limits; must match packages/shared/src/envelope.ts
MAX_FRAME_BYTES = 1_000_000
HEARTBEAT_TIMEOUT_S = 90
RECONNECT_MIN_S = 1
RECONNECT_MAX_S = 60
CHAT_TIMEOUT_S = 150
HELLO_TIMEOUT_S = 10

# --- Capability model: verbatim mirror of packages/shared/src/capabilities.ts (pinned by tests) ---
CAPABILITIES: tuple[str, ...] = (
    "entities.read",
    "services.read",
    "automations.read",
    "automations.write",
    "scenes.read",
    "scenes.write",
    "scripts.read",
    "scripts.write",
    "devices.control",
    "routines.run",
    "integrations.manage",
    "conversation",
)
# Capabilities that operate the home; off until the owner switches them on.
OPT_IN_CAPABILITIES: frozenset[str] = frozenset({"devices.control", "routines.run", "integrations.manage"})
# Capabilities the user toggles in the options UI (`conversation` is derived from the assistant mode).
TOGGLEABLE_CAPABILITIES: tuple[str, ...] = tuple(c for c in CAPABILITIES if c != "conversation")
CAPABILITY_FOR_METHOD: dict[str, str] = {
    "areas.list": "entities.read",
    "entities.list": "entities.read",
    "states.get": "entities.read",
    "services.list": "services.read",
    "automations.list": "automations.read",
    "automations.get": "automations.read",
    "automations.validate": "automations.write",
    "automations.create": "automations.write",
    "automations.update": "automations.write",
    "automations.delete": "automations.write",
    "scenes.list": "scenes.read",
    "scenes.get": "scenes.read",
    "scenes.create": "scenes.write",
    "scenes.update": "scenes.write",
    "scenes.delete": "scenes.write",
    "scripts.list": "scripts.read",
    "scripts.get": "scripts.read",
    "scripts.validate": "scripts.write",
    "scripts.create": "scripts.write",
    "scripts.update": "scripts.write",
    "scripts.delete": "scripts.write",
    "devices.call": "devices.control",
    "scenes.activate": "routines.run",
    "scripts.run": "routines.run",
    "integrations.list": "integrations.manage",
    "integrations.discovered": "integrations.manage",
    "integrations.available": "integrations.manage",
    "integrations.flow_start": "integrations.manage",
    "integrations.flow_step": "integrations.manage",
    "integrations.flow_abort": "integrations.manage",
}
CAPABILITIES_VERSION = 2

# --- Options (entry.options) ---
OPT_CONNECTION_ENABLED = "connection_enabled"
OPT_ASSISTANT_MODE = "assistant_mode"
OPT_MODEL = "model"
ASSISTANT_MODE_MANAGED = "managed"
ASSISTANT_MODE_BYO = "byo"
ASSISTANT_MODE_OFF = "off"
ASSISTANT_MODES: tuple[str, ...] = (ASSISTANT_MODE_MANAGED, ASSISTANT_MODE_BYO, ASSISTANT_MODE_OFF)
DEFAULT_MODELS: tuple[str, ...] = ("claude-opus-5", "claude-sonnet-5")
DEFAULT_MODEL = "claude-opus-5"
ACCOUNT_STATUS_TIMEOUT_S = 10


def option_key(capability: str) -> str:
    """entities.read -> cap_entities_read (the entry.options key)."""
    return "cap_" + capability.replace(".", "_")
