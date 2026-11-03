"""Entity registry ids, resolved to entity ids before an authored config is checked (#663).

Home Assistant accepts an entity's **registry id** — the 32-hex `uuid4().hex` it keeps in the entity
registry — wherever a config names an entity by `entity_id`: a service target (`cv.TARGET_SERVICE_FIELDS`,
resolved at call time by `helpers/service.py:305-311`), the `state`, `numeric_state` and `zone`
triggers, the `state` and `numeric_state` conditions, and the device actions, triggers and conditions
that name an entity (`cv.entity_id_or_uuid`). A registry id carries no domain, so the policy walkers, which read the
domain off the id, saw nothing: `homeassistant.turn_on` at a siren's registry id walked clean and HA
then called `siren.turn_on`.

**In an automation, script or scene the rule is keyed on `entity_id`.** Every config schema on the
pinned 2026.9.3 that accepts a registry id does so under a key named `entity_id`. The positions that look similar do not
accept one, so there is nothing there to resolve: `scene:` is `cv.entity_domain("scene")`, a scene's
`entities` keys are `cv.entity_id` (`homeassistant/scene.py:44`), and `snapshot_entities` is
`cv.entity_ids`. A value under any other key is left alone, and that is what keeps a `device_id` —
also 32 hex characters, and resolved against a different registry — untouched.

**A config-flow form is different, so `any_key=True` reads every value.** `EntitySelector` takes a
registry id in whatever field it backs (`helpers/selector.py:1115-1118`, which also skips the field's
`domain` filter for one), and the flow manager runs the form schema on the input
(`data_entry_flow.py:360`): `derivative`'s `source` and `generic_thermostat`'s `heater` take one as
readily as a field named `entity_id`. The reference walker reads every form value for the same reason
(AgDR-0038). There the `device_id` exception is made by asking: a 32-hex value that names a device in
the device registry is left alone, and only one that names neither is refused.

**An id that does not resolve is refused**, not passed. HA keeps a deleted entity's registry entry
and gives the same id back when the entity is restored, so an id that names nothing today can name a
siren tomorrow. The cost is a registry id written by hand into a service target, for an entity since
deleted: re-saving that config through Hearth is refused until the step is fixed, and so is every Hearth
automation that starts an owner's script holding one (`scenes.find_nested_script_violations`). A stale id
in a device action or condition costs nothing new: HA already disables a script holding one (it loads
`unavailable`, "Unknown entity").

**What is checked is the resolved copy; what is written is the caller's config, unchanged.** This
returns a new structure and never edits its input, so the owner's YAML keeps the ids HA's editor put
there.

The hub does **not** resolve, deliberately: it has no entity registry. Its walkers see a registry id
as an id with no domain and pass it, so the box is the only side that refuses one.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

# Mirror of `_FAKE_UUID_4_HEX` in `helpers/config_validation.py`, which is what `cv.entity_id_or_uuid`
# accepts as a registry id. Lower-case only, as there: an upper-case one fails HA's own validation.
_REGISTRY_ID = re.compile(r"[0-9a-f]{32}")


def resolve_registry_ids(hass: HomeAssistant, config: Any, path: str = "config", *, any_key: bool = False) -> tuple[Any, list[str]]:
    """`config` with every registry id under an `entity_id` key resolved, and the ids that did not.

    With `any_key`, for a config-flow form, every value is read, and a device id is left alone.
    Returns (resolved copy, problems). The copy is a new structure; `config` is not modified.
    """
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    problems: list[str] = []

    def resolve(value: Any, p: str) -> Any:
        """The entity a registry id names; anything else comes back as it was, untouched."""
        if not isinstance(value, str) or not _REGISTRY_ID.fullmatch(value.strip()):
            return value
        if (entity_id := er.async_resolve_entity_id(registry, value.strip())) is None:
            if any_key and devices.async_get(value.strip()) is not None:
                return value
            # The value is not repeated: under `any_key` it can be a 32-hex BLE key (`bindkey`, `key`,
            # `encryption_key`), and this message goes to the hub and into `audit_log.error`. `p` names the field.
            problems.append(f"{p}: a 32-hex id Home Assistant cannot resolve to an entity, so what it names cannot be checked")
            return value
        return entity_id

    def entity_ids(value: Any, p: str) -> Any:
        # Comma-split for a string, as `cv.entity_ids_or_uuids` is (see `policy._entity_ids`). Rejoined
        # only when an item changed, so a template's own commas are never rewritten in the copy.
        if isinstance(value, str):
            parts = value.split(",")
            resolved = [resolve(x, p) for x in parts]
            return value if resolved == parts else ", ".join(x.strip() for x in resolved)
        if isinstance(value, list):
            return [resolve(x, f"{p}[{i}]") for i, x in enumerate(value)]
        return copy(value, p)

    def copy(node: Any, p: str) -> Any:
        if isinstance(node, dict):
            return {k: entity_ids(v, f"{p}.{k}") if k == "entity_id" else copy(v, f"{p}.{k}") for k, v in node.items()}
        if isinstance(node, list):
            return [copy(v, f"{p}[{i}]") for i, v in enumerate(node)]
        return resolve(node, p) if any_key else node

    return copy(config, path), list(dict.fromkeys(problems))
