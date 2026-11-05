"""Diagnostics support (secrets redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HearthConfigEntry
from .const import CONF_INSTALL_SECRET, CONF_OWNER_EMAIL, CONF_PAIRING_TOKEN
from .options import HearthOptions
from .rpc import ALLOWED_METHODS

# The owner's email is personal data, and a diagnostics file is made to be shared.
TO_REDACT = {CONF_INSTALL_SECRET, CONF_PAIRING_TOKEN, CONF_OWNER_EMAIL}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: HearthConfigEntry) -> dict[str, Any]:
    client = entry.runtime_data.client
    options = HearthOptions.from_entry(entry)
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": options.to_dict(),
        "effective_capabilities": options.capabilities,
        "connection_enabled": options.connection_enabled,
        "connected": client.connected,
        "installation_id": client.installation_id,
        "last_error": client.last_error,
        "allowed_methods": sorted(ALLOWED_METHODS),
    }
