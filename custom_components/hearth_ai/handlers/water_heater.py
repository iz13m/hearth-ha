"""A water heater's exact ceiling, which only the box can apply (#560, AgDR-0075).

The walkers in `policy.py` and `policy.ts` know neither the home's unit nor the entity's range, so they
refuse a temperature that is not a literal number or is above 140, over the ceiling in either unit.
This module applies the real one: **60 °C, or 140 °F in a Fahrenheit home, or the entity's own
`max_temp` if that is lower.**

- The unit is `hass.config.units.temperature_unit`, the unit `water_heater.set_temperature` reads its
  number in (its handler converts from it to the entity's own unit) and the one the state machine
  reports `max_temp` in. It is the unit #557's app cap reads, through the `temperature_unit` the box
  reports beside a water heater.
- `max_temp` counts only as a literal number below the ceiling; a missing or odd one leaves the
  ceiling. A target this module cannot read as a literal water heater id (an area, a device, a
  group, a template) is held to the home's ceiling.
- **Never clamped.** A value over the ceiling is refused in words.

Applied to `devices.call` (`control.py`), every authored automation and script
(`scenes.find_config_violations`, both of `_validate`'s passes), a scene Hearth writes
(`scenes._validate`), every scene run, a household's own included (`scenes.find_scene_run_violations`,
which an authored `scene.turn_on` and a `devices.call` on a group holding a scene also ask), a water
heater a scene reaches through a `group.*` it sets (`scenes.group_refusals`, which hands the group's
stored attributes to every member as Home Assistant's group `reproduce_state` does), a `Hearth: scene`
script when it runs (`scripts.script_run_violations`), and a config-flow form value
(`flows.check_flow_input`).
A household script one hop away is not: AgDR-0005's accepted limit, as for remotes.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant

from ..policy import (
    _water_heater_scene_temperature_problems,
    TARGET_BEARING_KEYS,
    WATER_HEATER_CEILING,
    WATER_HEATER_CEILING_RULE,
    WATER_HEATER_DOMAIN,
    _domain,
    _entity_ids,
    _fold,
    is_literal_number,
)
from .remotes import _visit_services

_SET_TEMPERATURE = f"{WATER_HEATER_DOMAIN}.set_temperature"
_SCENE_SERVICES = ("scene.apply", "scene.create")


def ceiling(hass: HomeAssistant, entity_id: str | None = None) -> tuple[float, str]:
    """The highest temperature Hearth sets on `entity_id`, and the home's unit it is in."""
    unit = hass.config.units.temperature_unit
    limit: float = WATER_HEATER_CEILING["°F"] if unit == UnitOfTemperature.FAHRENHEIT else WATER_HEATER_CEILING["°C"]
    if isinstance(entity_id, str) and _domain(entity_id) == WATER_HEATER_DOMAIN:
        state = hass.states.get(entity_id.strip().lower())
        own = state.attributes.get("max_temp") if state is not None else None
        if is_literal_number(own) and own < limit:
            limit = own
    return limit, unit


def _shown(value: float) -> str:
    return f"{value:g}"


def temperature_refusal(hass: HomeAssistant, entity_id: str | None, value: Any) -> str | None:
    """Why `value` may not be set on `entity_id`, or None. A non-literal is the walkers' to refuse, and passes here."""
    if not is_literal_number(value):
        return None
    limit, unit = ceiling(hass, entity_id)
    if value <= limit:
        return None
    what = entity_id if isinstance(entity_id, str) else "this water heater"
    return f"{what}: {_shown(value)} {unit} is above {_shown(limit)} {unit}; {WATER_HEATER_CEILING_RULE}"


def _temperatures(node: dict[str, Any]) -> list[Any]:
    return [c["temperature"] for c in (node.get("data"), node.get("data_template")) if isinstance(c, dict) and "temperature" in c]


def _targets(node: dict[str, Any]) -> list[str | None]:
    """The literal ids a `set_temperature` node names, or `[None]` (the home's ceiling) when it names none."""
    ids = [i for k in TARGET_BEARING_KEYS for i in _entity_ids(node.get(k))] + _entity_ids(node)
    return list(dict.fromkeys(ids)) or [None]


def water_heater_call_violations(hass: HomeAssistant, domain: str, service: str, entity_ids: list[str], data: Any = None) -> list[str]:
    """The control path: `devices.call` names its targets as a literal list."""
    if f"{_fold(domain)}.{_fold(service)}" != _SET_TEMPERATURE or not isinstance(data, dict):
        return []
    full = f"{domain}.{service}"
    return [f"{full}: {why}" for eid in (entity_ids or [None]) if (why := temperature_refusal(hass, eid, data.get("temperature")))]


def scene_water_heater_violations(hass: HomeAssistant, entities: Any, path: str = "entities") -> list[str]:
    """Every water heater a scene's `entities` sets above its ceiling: a stored scene, or `scene.apply`'s."""
    if not isinstance(entities, dict):
        return []
    problems: list[str] = []
    for eid, value in entities.items():
        if _domain(eid) == WATER_HEATER_DOMAIN and isinstance(value, dict) and (why := temperature_refusal(hass, str(eid), value.get("temperature"))):
            problems.append(f"{path}.{why}")
    return problems


def group_water_heater_refusals(hass: HomeAssistant, entity_id: str, value: Any) -> list[str]:
    """Why a scene that sets a group holding `entity_id` to `value` may not run (#560).

    Home Assistant's group `reproduce_state` hands the group's stored state, attributes included, to
    every member, so `group.tanks: {state: eco, temperature: 75}` sets each water heater in it to 75.
    `value` is the group's stored entry, or anything else when the scene has no `scenes.yaml` entry and
    what the group is set to cannot be read: that is refused, as a valve is. A bare state carries no
    temperature. The mode is not checked: a household's own scene keeps its mode when it runs.
    """
    if isinstance(value, str):
        return []
    if not isinstance(value, dict):
        return ["Hearth cannot read the temperature this scene gives it, so it cannot be activated"]
    problems = list(_water_heater_scene_temperature_problems(value))
    if why := temperature_refusal(hass, entity_id, value.get("temperature")):
        problems.append(why)
    return problems


def find_water_heater_violations(hass: HomeAssistant, config: Any, path: str = "config") -> list[str]:
    """Every water heater temperature in an authored config, checked against its exact ceiling."""
    problems: list[str] = []

    def each(node: dict[str, Any], action: str, p: str) -> None:
        folded = _fold(action)
        if folded == _SET_TEMPERATURE:
            for value in _temperatures(node):
                problems.extend(f"{p}: action {action}: {why}" for eid in _targets(node) if (why := temperature_refusal(hass, eid, value)))
        if folded in _SCENE_SERVICES:
            for src in (node, node.get("data"), node.get("data_template")):
                if isinstance(src, dict):
                    problems.extend(f"{p}: action {action} sets {why}" for why in scene_water_heater_violations(hass, src.get("entities"), "entities"))

    _visit_services(config, path, each)
    return list(dict.fromkeys(problems))
