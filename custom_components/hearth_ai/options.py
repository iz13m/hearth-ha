"""User-managed settings stored in entry.options, with defaults in one place."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    ASSISTANT_MODE_MANAGED,
    OPT_IN_CAPABILITIES,
    ASSISTANT_MODES,
    OPT_ASSISTANT_MODE,
    OPT_CONNECTION_ENABLED,
    OPT_MODEL,
    TOGGLEABLE_CAPABILITIES,
    option_key,
)


@dataclass(frozen=True)
class HearthOptions:
    connection_enabled: bool = True
    assistant_mode: str = ASSISTANT_MODE_MANAGED
    model: str = ""
    enabled_caps: frozenset[str] = frozenset(c for c in TOGGLEABLE_CAPABILITIES if c not in OPT_IN_CAPABILITIES)

    @classmethod
    def from_mapping(cls, options: Any) -> HearthOptions:
        options = dict(options or {})
        mode = options.get(OPT_ASSISTANT_MODE, ASSISTANT_MODE_MANAGED)
        if mode not in ASSISTANT_MODES:
            mode = ASSISTANT_MODE_MANAGED
        return cls(
            connection_enabled=bool(options.get(OPT_CONNECTION_ENABLED, True)),
            assistant_mode=mode,
            model=str(options.get(OPT_MODEL) or ""),
            # Reading and authoring default on; operating the home defaults off.
            enabled_caps=frozenset(
                c for c in TOGGLEABLE_CAPABILITIES if bool(options.get(option_key(c), c not in OPT_IN_CAPABILITIES))
            ),
        )

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> HearthOptions:
        return cls.from_mapping(entry.options)

    @property
    def managed(self) -> bool:
        return self.assistant_mode == ASSISTANT_MODE_MANAGED

    @property
    def capabilities(self) -> list[str]:
        """Effective capability list announced to the hub (sorted, deterministic)."""
        caps = set(self.enabled_caps)
        if self.managed:
            caps.add("conversation")
        return sorted(caps)

    def to_dict(self) -> dict[str, Any]:
        return {
            OPT_CONNECTION_ENABLED: self.connection_enabled,
            OPT_ASSISTANT_MODE: self.assistant_mode,
            OPT_MODEL: self.model,
            **{option_key(c): c in self.enabled_caps for c in TOGGLEABLE_CAPABILITIES},
        }
