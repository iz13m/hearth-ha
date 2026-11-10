"""Which entities are entrances, which no assistant may open (R3 of the council's ruling of 2026-11-09, AgDR-0074).

The rule itself lives in `policy.py` (and `policy.ts`), as #527's valve rule does: a call, a scene state
or a device action on an entrance is allowed only if all it can do is close it. What neither walker can
know is **which ids are entrances**, because that is decided by things only this side can see:

- a `cover` whose `device_class` is garage, gate or door (`ENTRANCE_COVER_CLASSES`), read off the state
  and, failing that, the registry's own and original class;
- anything carrying the ordinary Home Assistant label `Hearth: entrance`, on the entity or on its own
  device (`labels.is_entrance_labelled`);
- a `switch`, `cover`, `button` or `input_button` whose name or entity id says garage, gate, كراج or
  بوابة (`ENTRANCE_NAME_RE`, `NAMED_DOMAINS`) — the ruling's default, because most garage openers reach
  Home Assistant as a relay (#613), a class-less cover or a button that pulses one. The ruling lets an
  admin clear a guessed one from the Hearth panel; **that override is not built yet**, so today a
  "garage lights" switch is an entrance too and may only be switched off by an assistant;
- every entity a UI-made **template** helper's actions name, when that helper is an entrance: a
  template cover of class garage whose `open_cover` calls `switch.relay` makes `switch.relay` the
  garage too. Read from the helper's config entry, so a template written in YAML is not followed:
  that is the stated limit, and the label's description says to label the relay as well;
- either half of a `switch_as_x` pair, when the other half is one of the above: a garage relay wrapped
  as a `cover` (with no class) is still the relay, and the relay under a garage-class wrapper is still
  the garage, which an authored `switch.turn_on` on the hidden relay would otherwise open;
- any group that holds one of those, followed as far as `expand_members` goes, because a call on a group
  is a call on each member. In a home with any entrance, a group whose membership cannot be walked (a
  cycle, or deeper or wider than the bounds) **counts as one**: Home Assistant follows a deep chain the
  walk gives up on, so `g10 -> ... -> garage` would otherwise be a way to open the garage.

`entrance_ids` answers for the whole home and is what every caller is handed. It is cached in
`hass.data` and dropped on every event that can change it (registry, label, state attribute, script
reload, template or `switch_as_x` entry), never on a timer, so a label added a moment ago counts at once.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, label_registry as lr
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ..labels import is_entrance_labelled
from ..policy import (
    _TEMPLATE_RE,
    ENTRANCE_COVER_CLASSES,
    ENTRANCE_RULE,
    _service_name,
    _target_selection,
    find_policy_violations,
    is_entrance_problem,
)
from .gated import _candidates, _nodes, _strings
from .registry import SWITCH_AS_X, MembershipUnwalkable, _source_of, expand_members, group_members

# Domains whose entities may be entrances by name alone (ruling (c), widened on review): a relay, a
# cover with no class, and a button that pulses an opener.
NAMED_DOMAINS: frozenset[str] = frozenset({"switch", "cover", "button", "input_button"})
TEMPLATE = "template"
_CACHE_KEY = "hearth_ai_entrance_cache"
_WATCH_KEY = "hearth_ai_entrance_watch"
_ROUTINES = frozenset({"scene", "script", "automation"})

# A word, not a substring: "gateway" and "tailgate" are not gates, "garage door" and "Gate 2" are.
# The Arabic words are matched anywhere, because Arabic attaches the article and prepositions
# (الكراج, بالبوابة) to the word itself, in the spellings households use (بوابة/بوابه, كراج/جراج/قراج).
ENTRANCE_NAME_RE = re.compile(
    r"(?<![a-z])(?:garage|gate)s?(?![a-z])|(?<![a-z])door\s*opener|(?<![a-z])roller\s*doors?(?![a-z])|كراج|جراج|قراج|بوابة|بوابه",
    re.IGNORECASE,
)
# A name that also says it is a light, a fan or a socket is that thing, not the opener: "Garage light",
# "إنارة الكراج" (review of 3a01d37). Which thing the name is about is its **head noun** (security's
# third round): English puts it last ("Garage light" is a light), Arabic first ("نور الكراج" is a
# light, "بوابة النور" is the Al-Noor gate). So an English exclusion word counts only when it comes
# after the entrance word and is the last word of the name, a trailing "switch" or number aside, so
# "Garage light switch" and "Front gate light switch 2" are light switches; an Arabic one only when it
# comes before the entrance word. A name that says opener, motor or relay is an entrance whatever
# follows (security's fourth round): "Garage door opener (light)", "Gate opener light", "Garage door
# lights relay". Pinned in `test_entrances.py`.
_NOT_AN_ENTRANCE_EN = re.compile(r"(?<![a-z])(?:lights?|lighting|lamps?|fans?|sockets?|plugs?)(?![a-z])", re.IGNORECASE)
_EN_TAIL = re.compile(r"[\W\d_]*(?:switch(?:es)?)?[\W\d_]*", re.IGNORECASE)
_OPENER_EN = re.compile(r"(?<![a-z])(?:openers?|motors?|relays?)(?![a-z])", re.IGNORECASE)
# Whole Arabic words (code review of e55c05e), with و or ف, then ال, بال or لل, attached in front.
_AR = "\u0600-\u06FF"
_NOT_AN_ENTRANCE_AR = re.compile(
    rf"(?<![{_AR}])[وف]?(?:ال|بال|لل)?(?:إنارة|انارة|ضوء|نور|لمبة|لمبه|مروحة|مروحه|فيش|أباجورة|اباجورة)(?![{_AR}])"
)
_TATWEEL = "\u0640"


def _cover_class(state: Any, ent: Any) -> str | None:
    cls = state.attributes.get("device_class") if state is not None else None
    if cls is None and ent is not None:
        cls = ent.device_class or ent.original_device_class
    value = getattr(cls, "value", cls)
    return value if isinstance(value, str) else None


def named_like_entrance(name: Any) -> bool:
    """Whether a name says garage, gate or an opener, and does not also say light, fan or socket."""
    if not isinstance(name, str):
        return False
    text = name.replace(_TATWEEL, "")
    found = list(ENTRANCE_NAME_RE.finditer(text))
    if not found:
        return False
    first, last = found[0].start(), found[-1].end()
    if any(m.start() < first for m in _NOT_AN_ENTRANCE_AR.finditer(text)):
        return False
    if _OPENER_EN.search(text):
        return True
    return not any(m.start() >= last and _EN_TAIL.fullmatch(text[m.end() :]) for m in _NOT_AN_ENTRANCE_EN.finditer(text))


def _user_facing_name(state: Any, ent: Any) -> Any:
    """The name the household sees: the state's, which follows a rename in Home Assistant.

    Not the entity id and not the integration's `original_name` on their own (review of 3a01d37), so
    renaming a wrongly guessed "Garage relay" to "Workshop lights" in Home Assistant clears it.
    """
    if state is not None:
        return state.name
    return (ent.name or ent.original_name) if ent is not None else None


def _named_like_entrance(entity_id: str, state: Any, ent: Any) -> bool:
    return named_like_entrance(_user_facing_name(state, ent))


def is_entrance_entity(hass: HomeAssistant, entity_id: str, ent_reg: er.EntityRegistry | None = None, dev_reg: dr.DeviceRegistry | None = None) -> bool:
    """Whether this one entity is an entrance by its own class, label or name. Groups, wrappers and relays are `entrance_ids`'s."""
    ent_reg = ent_reg or er.async_get(hass)
    entity_id = entity_id.lower()
    state = hass.states.get(entity_id)
    ent = ent_reg.async_get(entity_id)
    domain = entity_id.split(".", 1)[0]
    if domain == "cover" and _cover_class(state, ent) in ENTRANCE_COVER_CLASSES:
        return True
    if is_entrance_labelled(hass, ent, dev_reg):
        return True
    return domain in NAMED_DOMAINS and _named_like_entrance(entity_id, state, ent)


def _switch_as_x_pairs(hass: HomeAssistant, ent_reg: er.EntityRegistry) -> list[set[str]]:
    """Each `switch_as_x` source with every wrapper on it, as one set."""
    pairs: dict[str, set[str]] = {}
    for entry in hass.config_entries.async_entries(SWITCH_AS_X):
        if (source := _source_of(ent_reg, entry.options.get("entity_id"))) is None:
            continue
        same = pairs.setdefault(source, {source})
        same.update(e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id))
    return list(pairs.values())


def _templated_action(value: Any) -> bool:
    """Whether an action sequence names its service or its target with a template, so what it moves is unknowable."""
    for node, _p in _nodes(value, "options"):
        action = _service_name(node)
        if action is None:
            continue
        if not isinstance(action, str) or _TEMPLATE_RE.search(action):
            return True
        _keys, ids, unknowable = _target_selection(node, action)
        if unknowable or any(_TEMPLATE_RE.search(i) for i in ids):
            return True
    return False


def _template_actions(hass: HomeAssistant, ent_reg: er.EntityRegistry) -> list[tuple[set[str], set[str], bool, list[Any]]]:
    """Each UI-made template helper: its own entities, every existing entity its actions name, and
    whether an action names its service or target with a template, and the action values themselves,
    which the fixed point walks as it walks a script's config.

    Actions are the option values that are lists or mappings; a state or position template is a
    string and names what the helper *reads*, which is not what it moves. A templated action could
    move anything, so in a home with an entrance such a helper counts as one, as a group the walk
    cannot finish does (security's fourth round).
    """
    from .common import plain  # noqa: PLC0415
    from .resolve import resolve_registry_ids  # noqa: PLC0415

    out: list[tuple[set[str], set[str], bool, list[Any]]] = []
    for entry in hass.config_entries.async_entries(TEMPLATE):
        own = {e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}
        named: set[str] = set()
        opaque = False
        values: list[Any] = []
        for raw in entry.options.values():
            if not isinstance(raw, (list, dict)):
                continue
            # Resolved as a script's config is (security's sixth round): HA writes a device action's
            # `entity_id`, and a UI target, as an entity registry id. An id that does not resolve names
            # nothing that can be checked, so the helper is opaque, as an unwalkable group is.
            value, unresolved = resolve_registry_ids(hass, plain(raw), f"options.{entry.entry_id}")
            values.append(value)
            opaque = opaque or bool(unresolved) or _templated_action(value)
            for text in _strings(value):
                for candidate in _candidates(text):
                    eid = candidate.lower()
                    if hass.states.get(eid) is not None or ent_reg.async_get(eid) is not None:
                        named.add(eid)
        if own and values:
            out.append((own, named - own, opaque, values))
    return out


def _script_configs(hass: HomeAssistant) -> dict[str, Any]:
    """Every loaded script's config as Home Assistant holds it, registry ids resolved; unreadable ones left out."""
    from .resolve import resolve_registry_ids  # noqa: PLC0415
    from .scenes import _script_raw_config  # noqa: PLC0415 - scenes imports this module
    from .common import plain  # noqa: PLC0415

    out: dict[str, Any] = {}
    for state in hass.states.async_all("script"):
        raw = _script_raw_config(hass, state.entity_id)
        # Skipping `use_blueprint` is safe: only a script whose blueprint failed to load keeps the raw
        # reference here, and such a script is unavailable and cannot run.
        if isinstance(raw, dict) and "use_blueprint" not in raw:
            out[state.entity_id.lower()], _unresolved = resolve_registry_ids(hass, plain(raw), state.entity_id)
    return out


def _opens_an_entrance(config: Any, entrances: frozenset[str]) -> bool:
    """Whether the walk refuses this config for the entrance rule, given the entrances known so far."""
    return any(is_entrance_problem(v) for v in find_policy_violations(config, registry_ids_resolved=True, entrances=entrances))


def entrance_ids(hass: HomeAssistant) -> frozenset[str]:
    """Every entity id in this home that is an entrance, lower-cased. See the module docstring.

    Kept in `hass.data` and dropped by `_watch` on every event that can change the answer (code review
    of cec104a), so a busy home walks its scripts once per change rather than once per call, and a
    label added a moment ago still counts at once.
    """
    _watch(hass)
    cached = hass.data.get(_CACHE_KEY)
    if cached is None:
        cached = hass.data[_CACHE_KEY] = _compute_entrance_ids(hass)
    return cached


# State attributes the answer reads: a cover's class, the name rule's name, a group's members.
_WATCHED_ATTRIBUTES = ("device_class", "friendly_name", "entity_id")


@callback
def _watch(hass: HomeAssistant) -> None:
    """Drop the cached set on anything that can change it. Registered once per Home Assistant run.

    The listeners are kept for the whole run on purpose, across reloads and removal of the integration:
    `_WATCH_KEY` keeps them from being registered twice, each one only pops a key from `hass.data`, and
    unsubscribing in `async_unload_entry` would open a window in which a stale set could be served by
    the next entry's first call before its listeners were back.
    """
    if _WATCH_KEY in hass.data:
        return

    @callback
    def drop(*_args: Any) -> None:
        hass.data.pop(_CACHE_KEY, None)

    @callback
    def on_state(event: Event[Any]) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None or any(old.attributes.get(k) != new.attributes.get(k) for k in _WATCHED_ATTRIBUTES):
            drop()

    @callback
    def on_service(event: Event[Any]) -> None:
        # A script reload replaces every script's config; scenes are not part of the set.
        if event.data.get("domain") == "script" and event.data.get("service") == "reload":
            drop()

    @callback
    def on_entry(_change: Any, entry: Any) -> None:
        if entry.domain in (TEMPLATE, SWITCH_AS_X):
            drop()

    bus = hass.bus
    hass.data[_WATCH_KEY] = [
        bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, drop),
        bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, drop),
        bus.async_listen(lr.EVENT_LABEL_REGISTRY_UPDATED, drop),
        bus.async_listen(EVENT_STATE_CHANGED, on_state),
        bus.async_listen(EVENT_CALL_SERVICE, on_service),
        async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, on_entry),
    ]


def _compute_entrance_ids(hass: HomeAssistant) -> frozenset[str]:
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    candidates = {s.entity_id.lower() for s in hass.states.async_all()} | set(ent_reg.entities)
    found = {eid for eid in candidates if is_entrance_entity(hass, eid, ent_reg, dev_reg)}
    if not found:
        return frozenset()
    pairs = _switch_as_x_pairs(hass, ent_reg)
    templates = _template_actions(hass, ent_reg)
    groups = [s.entity_id.lower() for s in hass.states.async_all() if group_members(hass, s.entity_id)]
    scripts = _script_configs(hass)
    # To a fixed point, because each relation can feed the others: a template's relay may be a
    # wrapper's source, a group may hold a script that opens the garage, and a script may start a
    # group, a template switch or another script that does.
    while True:
        before = len(found)
        for same in pairs:
            if same & found:
                found |= same
        for own, named, opaque, values in templates:
            if own & found:
                found |= named
            # And the other way (re-review of e55c05e): a template whose actions drive an entrance is one,
            # and so is one whose actions cannot be read before they run. Its actions are also judged
            # exactly as a script's config is (security's fifth round), so one that opens an entrance
            # by area, device, label, floor, `all` or a device action is one too.
            if (own - found) and (named & found or opaque or any(_opens_an_entrance(v, frozenset(found)) for v in values)):
                found |= own
        for eid in groups:
            if eid in found:
                continue
            try:
                if found.intersection(expand_members(hass, eid, stop=_ROUTINES)):
                    found.add(eid)
            except MembershipUnwalkable:
                # Home Assistant follows a chain the walk gives up on, so in a home with an entrance one
                # that cannot be walked may hold one (review of 3a01d37).
                found.add(eid)
        # A script whose own config would open an entrance is one (security's third round): then
        # `homeassistant.turn_on script.open_garage`, a group holding it and a template switch over it
        # are all an entrance's id, and every id check already covers them.
        for eid, config in scripts.items():
            if eid not in found and _opens_an_entrance(config, frozenset(found)):
                found.add(eid)
        if len(found) == before:
            break
    return frozenset(found)


def entrance_of(hass: HomeAssistant, entity_id: str) -> bool:
    """`entity_id in entrance_ids(hass)`, for a caller asking about one entity at a time (the state push)."""
    return entity_id.lower() in entrance_ids(hass)


def find_entrance_named(hass: HomeAssistant, config: Any, path: str = "input", entrances: frozenset[str] | None = None) -> list[str]:
    """Why a config-flow form value may not be saved: it names an entrance (review of 3a01d37).

    A group, a template or a `switch_as_x` wrapper over an entrance is a new entity the walk has never
    seen, so an open authored on its future id passes, and creating the helper then hands the model a
    way to open the entrance. The form is where the new entity is made, so a form value that names an
    entrance anywhere — a member list, an action, a template — is refused, as `find_gate_violations`
    refuses a gated relay in `form` mode (#643). Reading an entrance's state in a template is refused
    with it; that is the cost of not telling a reading template from a moving one.
    """
    entrances = entrance_ids(hass) if entrances is None else entrances
    if not entrances:
        return []
    problems = []
    for text in _strings(config):
        for candidate in _candidates(text):
            if candidate.lower() in entrances:
                problems.append(f"{path}: {candidate} is an entrance, so Hearth will not write it into a helper or integration form; {ENTRANCE_RULE}")
    return list(dict.fromkeys(problems))


def entry_holds_entrance(hass: HomeAssistant, entry: Any, entrances: frozenset[str] | None = None) -> bool:
    """Whether removing this helper could unmake an entrance (review of 3a01d37), as `entry_carries_gate` asks for a gate.

    Any of its entities is an entrance, or it is a `switch_as_x` wrapper whose source is one: deleting
    the wrapper unhides the relay, copies the wrapper's exposure onto it, and leaves a switch whose own
    name may not say what it opens.
    """
    entrances = entrance_ids(hass) if entrances is None else entrances
    if not entrances:
        return False
    ent_reg = er.async_get(hass)
    if any(e.entity_id in entrances for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
        return True
    if entry.domain == SWITCH_AS_X:
        source = _source_of(ent_reg, entry.options.get("entity_id"))
        return source is not None and source in entrances
    return False
