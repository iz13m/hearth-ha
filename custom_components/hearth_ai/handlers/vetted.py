"""Which device a vetted integration service reaches, which only the box can tell (R4, AgDR-0076).

The walkers in `policy.py` and `policy.ts` refuse an integration's own service or device action until
it is vetted (`VETTED_INTEGRATION_SERVICES`, `VETTED_DEVICE_ACTIONS`). A vetted one still addresses a
device by its registry id, its Z-Wave node or its Zigbee IEEE address, or through one of its
entities, and the same device can hold a lock: that is #581's whole finding. So this module resolves
what a vetted call addresses to devices and refuses it when **any** of them holds a lock, alarm panel,
siren, camera or valve entity, or an entrance (`handlers/entrances.py`). Effective domains count, so a
switch a `switch_as_x` helper presents as a lock makes its device one that holds a lock (#643).

It fails closed: a `device_id` Home Assistant does not have, a node or IEEE address no device answers
to, or a value that is neither a string nor a number is refused, since what it would reach cannot be
checked. For a vetted integration service a templated value is the walker's to refuse, not repeated here.

**Any service or device action that names a device by address is asked the same question** (security
review of R4). `switch.turn_off` with `target: {device_id: <the lock's device>}` reaches every switch
on that device, which on a Z-Wave or Zigbee lock is its auto-relock or one-touch-locking setting, and a
`select` device action reaches its operating mode. The entity rules never see those: they name no
entity. So an authored service call or device action in any domain that addresses a device by
`device_id`, `node_id` or `ieee` (top level or `target`, and `data` unless the service is a script's,
whose `data` is the script's own variables) is refused when that device holds a lock, alarm panel,
siren, camera, valve or entrance, or when the address is a template. An address no device has is left
alone there: an entity service at a `device_id` Home Assistant does not know reaches nothing, and a
registry id is a random one that cannot later turn out to be a lock's. A vetted integration service is
stricter, since a node or an IEEE address can be a device paired tomorrow. Its
entity ids are left to the entity rules, as before. Triggers and conditions only read, and are not
asked. `devices.call` needs none of this: it takes entity ids only, and `_clean_data` strips the rest.

**What it cannot read, it refuses where a guarded device could be reached** (security re-reviews). A
`target`, `data` or `data_template` that is itself a template, or has a templated key, can render to
`{device_id: <the lock's device>}`, and Home Assistant really sends it; an `area_id`, `floor_id` or
`label_id` reaches every device in it, `entity_id: all` every entity of the domain, and a templated
`entity_id` any one of them. Neither is resolved here, deliberately: an area's contents change
when someone moves a lock, and refusing what cannot be checked is the only answer that stays true after
the config is written (`find_fan_out_violations` says the same). So a non-script action that selects
either way is refused when the home has a guarded device holding an entity in the action's own domain,
the only entities an entity service reaches (`homeassistant.*` reaches any). A home with no lock,
alarm panel, siren, camera, valve or entrance loses nothing, and neither does `light.turn_on` by area
in a home whose locks carry no light.

**Known limit, as in `gated.py`:** that set of domains is read when the config is written. A lock paired
later, or a guarded device that gains an entity in a new domain, can bring an automation written earlier
into reach of it, and nothing re-checks the automation then.

Applied to `devices.call` (`control.py`), every authored automation and script
(`scenes.find_config_violations`, both of `_validate`'s passes) and a config-flow form value
(`flows.check_flow_input`). Not to what the household wrote: a script one hop away or a labelled
script when it runs, which is also where the walkers leave the vetting rule out.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..policy import (
    DEVICE_ADDRESS_KEYS,
    ENTITY_SERVICE_DOMAINS,
    VETTED_DEVICE_ACTIONS,
    _TEMPLATE_RE,
    _TRIGGER_KEYS,
    _domain,
    _target_selection,
    _entity_ids,
    _fold,
    _service_name,
    is_vetted_service,
)
from .entrances import entrance_ids
from .registry import _gates, effective_domain

# Entity domains whose device an integration's own command may not reach.
GUARDED_DOMAINS: frozenset[str] = frozenset({"lock", "alarm_control_panel", "siren", "camera", "valve"})
GUARDED_DEVICE_RULE = (
    "Azka never lets an assistant send an integration's own command to a device that has a lock, alarm panel, siren, camera, "
    "valve or entrance; use that device's own controls in Home Assistant"
    " / لا يسمح أذكى للمساعد بإرسال أمر خاص بالتكامل إلى جهاز فيه قفل أو لوحة إنذار أو صفارة أو كاميرا أو صمام أو مدخل؛ "
    "استخدم عناصر التحكم الخاصة بالجهاز في Home Assistant"
)
# The any-domain path's refusal: an ordinary service or device action that addresses a device, not an
# integration's own command, so it says what to do instead.
GUARDED_ADDRESS_RULE = (
    "Azka never lets an assistant address a device that has a lock, alarm panel, siren, camera, valve or entrance as a whole; "
    "name the entities to act on instead"
    " / لا يسمح أذكى للمساعد بمخاطبة جهاز فيه قفل أو لوحة إنذار أو صفارة أو كاميرا أو صمام أو مدخل كاملاً؛ "
    "اذكر الكيانات المطلوبة بدلاً من ذلك"
)
_ZWAVE = "zwave_js"
_ZHA = "zha"


def guarded_devices(hass: HomeAssistant) -> frozenset[str]:
    """Every device that holds a lock, alarm panel, siren, camera, valve or entrance, by its own or effective domain."""
    ent_reg = er.async_get(hass)
    gates = _gates(hass)
    entrances = entrance_ids(hass)
    out: set[str] = set()
    for entry in ent_reg.entities.values():
        if not entry.device_id or entry.device_id in out:
            continue
        if entry.domain in GUARDED_DOMAINS or entry.entity_id.lower() in entrances or effective_domain(hass, entry.entity_id, gates) in GUARDED_DOMAINS:
            out.add(entry.device_id)
    return frozenset(out)


def _guarded_domains(hass: HomeAssistant, guarded: frozenset[str]) -> frozenset[str]:
    """Every entity domain present on a guarded device: what an entity service by area or template could reach there."""
    return frozenset(e.domain for e in er.async_get(hass).entities.values() if e.device_id in guarded)


_SELECTION_KEYS = ("area_id", "floor_id", "label_id")
_ALL_IDS = frozenset({"all", "none"})


def _unreadable_entity_id(node: dict[str, Any]) -> bool:
    """An `entity_id` that is neither a string nor a list of strings, in any carrier: in `_validate`'s second
    walk a templated id is a `Template` object, which `_entity_ids` does not return."""
    for src in (node, node.get("target"), node.get("data"), node.get("data_template")):
        if isinstance(src, dict) and "entity_id" in src:
            value = src["entity_id"]
            if isinstance(value, list):
                if any(not isinstance(v, str) for v in value):
                    return True
            elif not isinstance(value, str):
                return True
    return False
# Reaches entities of every domain, so any guarded device counts.
_ANY_DOMAIN_SERVICES = frozenset({"homeassistant"})
_SELECTION_RULE = (
    "this home has a lock, alarm panel, siren, camera, valve or entrance whose device holds an entity this service can reach, "
    f"and Azka cannot tell before it runs whether it would; {GUARDED_ADDRESS_RULE}"
)


def _by_identifier(hass: HomeAssistant, domain: str, match: Any) -> set[str]:
    """Devices with an identifier of `domain` for which `match(value)` holds."""
    return {d.id for d in dr.async_get(hass).devices.values() if any(i_domain == domain and match(str(value)) for i_domain, value in d.identifiers)}


def _addressed(
    hass: HomeAssistant, entity_ids: list[str], sources: list[Any], *, refuse_templates: bool = False, refuse_unknown: bool = True
) -> tuple[set[str], list[str]]:
    """The devices a call addresses, and why any of its addresses could not be resolved.

    `refuse_templates`: a templated address is unresolved here, for a caller whose walker does not
    refuse it already (anything but a vetted integration service). `refuse_unknown`: an address no
    device answers to is unresolved (a vetted integration service), rather than reaching nothing.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    devices: set[str] = set()
    unresolved: list[str] = []
    for eid in entity_ids:
        # An entity with no registry entry, or no device, is skipped rather than refused: it has no
        # device to share with a lock (a YAML or template entity, a group), and the entity rules have
        # already read its own domain. A templated id is the walker's to refuse.
        if isinstance(eid, str) and not _TEMPLATE_RE.search(eid) and (entry := ent_reg.async_get(eid.strip().lower())) and entry.device_id:
            devices.add(entry.device_id)
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in DEVICE_ADDRESS_KEYS:
            if key not in src:
                continue
            for value in src[key] if isinstance(src[key], list) else [src[key]]:
                if isinstance(value, str) and _TEMPLATE_RE.search(value):
                    if refuse_templates:
                        unresolved.append(f"{key} {value} is a template")
                    continue  # otherwise the walker's to refuse
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    unresolved.append(f"{key} {value!r} is not an address Azka can check")
                    continue
                text = str(value).strip()
                if key == "device_id":
                    found = {text} if dev_reg.async_get(text) else set()
                elif key == "node_id":
                    # Z-Wave JS names a node `<home id>-<node id>`, with the product appended on a second identifier.
                    found = _by_identifier(hass, _ZWAVE, lambda v, n=text: v.split("-")[1:2] == [n])
                else:
                    found = _by_identifier(hass, _ZHA, lambda v, ieee=text.lower(): v.lower() == ieee)
                if not found and refuse_unknown:
                    unresolved.append(f"{key} {text} is not a device Home Assistant has")
                devices |= found
    return devices, unresolved


def _problems(
    hass: HomeAssistant,
    what: str,
    p: str,
    entity_ids: list[str],
    sources: list[Any],
    guarded: frozenset[str],
    *,
    refuse_templates: bool = False,
    refuse_unknown: bool = True,
    rule: str = GUARDED_DEVICE_RULE,
) -> list[str]:
    devices, unresolved = _addressed(hass, entity_ids, sources, refuse_templates=refuse_templates, refuse_unknown=refuse_unknown)
    problems = [f"{p}: {what}: {u}, so what it reaches cannot be checked; {rule}" for u in unresolved]
    if devices & guarded:
        problems.append(f"{p}: {what} reaches a device that has a lock, alarm panel, siren, camera, valve or entrance; {rule}")
    return problems


def _integration_service(action: Any) -> bool:
    """A vetted service whose domain is an integration's, not an entity platform's."""
    return isinstance(action, str) and not _TEMPLATE_RE.search(action) and _domain(action) not in ENTITY_SERVICE_DOMAINS and is_vetted_service(action)


def vetted_call_violations(hass: HomeAssistant, domain: str, service: str, entity_ids: list[str], data: Any) -> list[str]:
    """Why a `devices.call` of a vetted integration service may not go to these entities. Empty means it may."""
    full = f"{domain}.{service}"
    if not _integration_service(full):
        return []
    return _problems(hass, "this call", full, list(entity_ids), [data], guarded_devices(hass))


def _names_a_device(sources: list[Any]) -> bool:
    return any(isinstance(src, dict) and any(key in src for key in DEVICE_ADDRESS_KEYS) for src in sources)


def find_vetted_target_violations(hass: HomeAssistant, config: Any, path: str = "config") -> list[str]:
    """Which device each action in an authored config or a form value reaches (see the module docstring).

    A vetted integration service or device action by everything it names; any other service call or
    device action by the devices it addresses. Not under a trigger key, and not a condition.
    """
    problems: list[str] = []
    guarded: frozenset[str] | None = None
    domains: frozenset[str] | None = None

    def reaches_guarded(action: str) -> bool:
        """Whether an action in this domain could reach an entity on a guarded device."""
        nonlocal guarded, domains
        if guarded is None:
            guarded = guarded_devices(hass)
        if not guarded:
            return False
        if domains is None:
            domains = _guarded_domains(hass, guarded)
        dom = _domain(action)
        return dom in _ANY_DOMAIN_SERVICES or dom in domains

    def ask(what: str, p: str, ids: list[str], sources: list[Any], *, vetted: bool) -> None:
        """`vetted`: a vetted integration service or device action, whose walker refuses a template
        already and whose unknown address could be tomorrow's device; else the any-domain check."""
        nonlocal guarded
        if guarded is None:
            guarded = guarded_devices(hass)
        rule = GUARDED_DEVICE_RULE if vetted else GUARDED_ADDRESS_RULE
        problems.extend(_problems(hass, what, p, ids, sources, guarded, refuse_templates=not vetted, refuse_unknown=vetted, rule=rule))

    def walk(node: Any, p: str, in_trigger: bool = False) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                walk(n, f"{p}[{i}]", in_trigger)
            return
        if not isinstance(node, dict):
            return
        if not in_trigger and "condition" not in node:
            action = _service_name(node)
            domain, kind = node.get("domain"), node.get("type")
            if _integration_service(action):
                ids = [i for k in ("target", "data", "data_template") for i in _entity_ids(node.get(k))] + _entity_ids(node)
                ask(f"action {action}", p, ids, [node, node.get("target"), node.get("data"), node.get("data_template")], vetted=True)
            elif isinstance(action, str) and not _TEMPLATE_RE.search(action):
                # A script's `data` is its variables, not a target: what it does with them is checked as a script.
                carriers = [node, node.get("target")] + ([] if _domain(action) == "script" else [node.get("data"), node.get("data_template")])
                if _names_a_device(carriers):
                    ask(f"action {action}", p, [], carriers, vetted=False)
                # What it selects without naming a device: a templated carrier, or an area, floor or label.
                if _domain(action) != "script":
                    keys, ids, unknowable = _target_selection(node, action)
                    by = [k for k in _SELECTION_KEYS if k in keys]
                    # `entity_id: all` (or `none`) is every entity of the domain, and a templated id is any
                    # one of them: both select like an area (third security review).
                    if any(_TEMPLATE_RE.search(i) or i.strip().lower() in _ALL_IDS for i in ids) or _unreadable_entity_id(node):
                        by.append("entity_id")
                    if (unknowable or by) and reaches_guarded(action):
                        how = "a target Azka cannot read until it runs" if unknowable else f"{', '.join(by)}"
                        problems.append(f"{p}: action {action} selects by {how}; {_SELECTION_RULE}")
            elif isinstance(domain, str) and "device_id" in node and action is None:
                vetted = isinstance(kind, str) and f"{_fold(domain)}.{_fold(kind)}" in VETTED_DEVICE_ACTIONS
                ask(f"device action {domain}.{kind}", p, [], [node], vetted=vetted)
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                walk(v, f"{p}.{k}", in_trigger or k in _TRIGGER_KEYS)

    walk(config, path)
    return list(dict.fromkeys(problems))
