"""Constants for the Azka AI integration."""

from __future__ import annotations

import json
from pathlib import Path

DOMAIN = "hearth_ai"
# Read from the manifest (loaded in an executor at import time) so the version we report to the
# hub can never drift from the version HACS installed.
INTEGRATION_VERSION: str = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))["version"]

# What Devices & services shows for this entry. Home Assistant stores the title when the entry is
# created and never re-reads `manifest.json` for it, so a rename of the manifest alone would leave
# every already-paired home reading "Hearth AI" forever while new ones read "Azka AI" — and every
# instruction of the form "Settings -> Devices & services -> <name> -> Configure" would then have
# no single correct value. `async_setup_entry` migrates the old default; see there for why only
# the default (AgDR-0063).
ENTRY_TITLE = "Azka AI"
LEGACY_ENTRY_TITLE = "Hearth AI"

CONF_HUB_URL = "hub_url"
CONF_PAIRING_TOKEN = "pairing_token"
CONF_INSTALL_SECRET = "install_secret"
CONF_INSTALLATION_ID = "installation_id"
CONF_WS_URL = "ws_url"

DEFAULT_HUB_URL = "https://app.example.com"

# --- Claiming a home (AgDR-0064): a mirror of `claim` in packages/shared/schema/methods.json,
# pinned by tests/test_claim.py. Never imported from the Node side at runtime.
# A box that registered without a pairing token stores `claimed: False` until an HA admin approves a
# claim in the panel. An entry with no `claimed` key was paired with a token, and is claimed.
CONF_CLAIMED = "claimed"
CONF_UNCLAIMED_EXPIRES_AT = "unclaimed_expires_at"
# Who approved claim became the owner, as the hub named them in `claim.approve`'s answer. Shown by the
# panel; the hub has no method that reports a claimed home's owner, so a token-paired home has none.
CONF_OWNER_EMAIL = "owner_email"
# False while a home claimed through the panel waits for its owner to confirm it in the app, as the
# last hello said. Kept so a restart does not start out believing an unconfirmed home is live.
# Absent means confirmed (every token-paired entry, and every hub older than the confirmation).
CONF_CONFIRMED = "confirmed"
UNCLAIMED_HA_TO_HUB_METHODS: frozenset[str] = frozenset({"hello", "claim.lookup", "claim.approve", "claim.decline"})
CLAIM_TOKEN_PATTERN = r"hhc_[A-Za-z0-9_-]{22}"
CLAIM_LINK_PREFIX = "hearth://claim/"
# What `claim.approve`'s `new_secret` must look like: the box makes its own next secret in the
# shape the hub mints them.
INSTALL_SECRET_PATTERN = r"his_[A-Za-z0-9_-]{43}"
CLAIMED_CLOSE_CODE = 4010
UNCLAIMED_EXPIRED_CLOSE_CODE = 4011
# The owner confirmed a claimed home in the app: reconnect on the same secret, and the home is live.
CLAIM_CONFIRMED_CLOSE_CODE = 4012
# The hub deleted this installation: a live home its owner removed, or a claimed home whose owner
# answered "not my home" in the app. Its secret is dead; only registering again helps. Pinned to
# `claim.installation_removed_close_code` in the shared schema by tests/test_claim.py.
INSTALLATION_REMOVED_CLOSE_CODE = 4003
CLAIM_TIMEOUT_S = 15

# Wire-level limits; must match packages/shared/src/envelope.ts
MAX_FRAME_BYTES = 1_000_000
HEARTBEAT_TIMEOUT_S = 90
RECONNECT_MIN_S = 1
RECONNECT_MAX_S = 60
CHAT_TIMEOUT_S = 150
HELLO_TIMEOUT_S = 10
# A pushed state change is worthless once it is stale, so it waits far less than a chat turn.
PUSH_TIMEOUT_S = 10

# --- Capability model: verbatim mirror of packages/shared/src/capabilities.ts (pinned by tests) ---
CAPABILITIES: tuple[str, ...] = (
    "entities.read",
    "entities.subscribe",
    "entities.history",
    "helpers.manage",
    "labels.manage",
    "notify.send",
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
    "areas.manage",
    "entities.manage",
    "access.control",
    "vision.view",
    "vision.live",
    "conversation",
)
# Capabilities that wait for the owner's yes. Most of them operate the home; `entities.history` is
# only a read, and is here because a month of readings shows when the house is empty (AgDR-0036).
OPT_IN_CAPABILITIES: frozenset[str] = frozenset(
    {"devices.control", "routines.run", "integrations.manage", "areas.manage", "entities.manage", "access.control", "vision.view", "vision.live", "entities.history", "helpers.manage", "labels.manage", "notify.send"}
)
# Capabilities the user toggles in the options UI (`conversation` is derived from the assistant mode).
TOGGLEABLE_CAPABILITIES: tuple[str, ...] = tuple(c for c in CAPABILITIES if c != "conversation")
CAPABILITY_FOR_METHOD: dict[str, str] = {
    "access.list": "access.control",
    "access.operate": "access.control",
    "vision.list": "vision.view",
    "vision.snapshot": "vision.view",
    "vision.webrtc_config": "vision.live",
    "vision.webrtc_offer": "vision.live",
    "vision.webrtc_candidate": "vision.live",
    "vision.webrtc_close": "vision.live",
    "entities.exposable": "entities.manage",
    "entities.expose": "entities.manage",
    "areas.list": "entities.read",
    "floors.list": "entities.read",
    "areas.create": "areas.manage",
    "areas.set_floor": "areas.manage",
    "floors.create": "areas.manage",
    "areas.update": "areas.manage",
    "areas.delete": "areas.manage",
    "floors.update": "areas.manage",
    "floors.delete": "areas.manage",
    "areas.assign": "areas.manage",
    "entities.list": "entities.read",
    "states.get": "entities.read",
    # Hearth's own arrangement, not the home (AgDR-0046). No new capability: the panel that writes
    # it is local to Home Assistant and admin-only, and this read carries no state.
    "presentation.get": "entities.read",
    "states.history": "entities.history",
    "helpers.types": "helpers.manage",
    "helpers.describe": "helpers.manage",
    "helpers.list": "helpers.manage",
    "helpers.create": "helpers.manage",
    "helpers.update": "helpers.manage",
    "helpers.rename": "helpers.manage",
    "helpers.delete": "helpers.manage",
    "labels.list": "labels.manage",
    "labels.create": "labels.manage",
    "labels.update": "labels.manage",
    "labels.delete": "labels.manage",
    "labels.assign": "labels.manage",
    "categories.list": "labels.manage",
    "categories.create": "labels.manage",
    "categories.update": "labels.manage",
    "categories.delete": "labels.manage",
    "categories.assign": "labels.manage",
    "notify.targets": "notify.send",
    "notify.send": "notify.send",
    "services.list": "services.read",
    "automations.list": "automations.read",
    "automations.get": "automations.read",
    "automations.validate": "automations.write",
    "automations.create": "automations.write",
    "automations.update": "automations.write",
    "automations.delete": "automations.write",
    # Switching a rule on or off is a write to an automation, not a new kind of reach: no new
    # capability, so no CAPABILITIES_VERSION bump (AgDR-0044).
    "automations.set_enabled": "automations.write",
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
