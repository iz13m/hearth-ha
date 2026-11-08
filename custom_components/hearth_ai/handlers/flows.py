"""Driving Home Assistant's config flows: serialising a form, and policing what comes back.

Shared by `handlers/integrations.py` (adding a device or service) and, from AgDR-0039, by
`handlers/helpers.py` (making a helper), which are the same machinery pointed at different halves of
`async_get_config_flows`.

**A form field is an authoring surface (AgDR-0038).** Home Assistant's `template` helper carries
`selector.ActionSelector()` fields — `turn_on`, `turn_off`, `press` — and `ActionSelector.__call__`
returns whatever it is given without validating it. Whatever is put there really runs when the entity
is operated, and a template switch lands in `switch`, which Home Assistant exposes to Assist by
default. So submitting a form was a way to author an action sequence the automation path would have
refused, and then fire it with `devices.call` or a time-triggered automation. `check_flow_input`
applies the same policy to the same shape; the hub refuses the same values before they ever leave.

**And a form we showed is the only description of it we have.** `flow_step` used to consult
`flow.get("data_schema")` to catch a password-typed field with an innocent name, but
`async_progress()` builds its dicts from `flow_id`, `handler`, `context` and `step_id` only — so that
was always `None` and the check was dead code. `remember_fields` keeps our own serialisation instead.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv, entity_registry as er

from ..const import DOMAIN
from ..policy import find_policy_violations, find_reference_violations
from ..rpc import RpcError
from .gated import find_gate_violations
from .remotes import find_remote_violations
from .resolve import resolve_registry_ids
from .scenes import find_nested_authoring_violations
from .registry import SWITCH_AS_X, _gates, _source_of, visible

_LOGGER = logging.getLogger(__name__)

# Integrations Hearth will not set up on the user's behalf: they grant host-level power, or
# they are Hearth itself.
DENIED_DOMAINS: frozenset[str] = frozenset(
    {
        "hearth_ai",
        "hassio",
        "backup",
        "homeassistant",
        "command_line",
        "shell_command",
        "python_script",
        "ffmpeg",
        # Writes attacker-chosen bytes to an attacker-chosen path (#227). Its config flow asks only
        # for a download directory and validates that the directory *exists* — so a model set it to
        # the config directory and then overwrote `automations.yaml` with an automation that
        # unlocked a door, which never passed a policy walker because it never went through the
        # write path they sit on. Unlike every other file-writing integration, `downloader` decides
        # its directory at **setup** rather than per call, which is why HA's own
        # `is_allowed_path` never comes into it. See `test_every_path_writing_service_is_classified`.
        "downloader",
        # Same shape as `downloader`, on the same axis: its config flow takes `CONF_FILE_PATH`
        # validated by `os.access` alone (`local_file/util.py:8`) — no `is_allowed_path` — and the
        # resulting `camera.*` entity serves that file's bytes. The sibling `file` integration
        # *does* gate its flow, so "Home Assistant will catch it" is not available here.
        #
        # Found because the classifier could not see this axis at all: it enumerates
        # `*/services.yaml`, which is services only. A PR arguing "two axes, because one output
        # hides half the class" had an instrument for one of them (#227 review).
        "local_file",
        # A third shape on the same axis: `CONF_FILE_PATH` is a raw
        # `str` with no validator at all (distinct from the `SerialPortSelector` device field
        # beside it), handed to `upb_lib` as `UPStartExportFile`.
        #
        # `config_ok` tests only that the path exists and is readable: `process_upstart_file`
        # returns False solely on OSError, and a malformed file logs and returns early **without
        # raising**, so the function still returns True. `/etc/hosts` passes.
        #
        # **What it is: a readability oracle for an arbitrary file, and disclosure only from a
        # file the caller could already write.** An earlier version of this comment said "past
        # that check the file's own bytes become entity names ... strictly worse than
        # `local_file`". That is false, and backwards. `_process_file` requires line 1 to satisfy
        # `fields[0] == BOF` (`"0"`), and thereafter only lines whose first field is a record type
        # (`"2"` link, `"3"` device) contribute anything. An arbitrary secret has neither:
        #
        #     /etc/shadow style   -> returned=True  entities=[]
        #     .env with a token   -> returned=True  entities=[]
        #     crafted UPE file    -> returned=True  entities=['SECRET_FROM_HOST_FILE']
        #
        # So for a secret the caller does not control, `upb` answers only *does this path exist
        # and can Home Assistant read it*. `local_file` is the one that serves a readable file's
        # bytes; on that axis it is the more severe of the two, not the less. The error was
        # generalising from a run against a **crafted** file — a valid demonstration of what a
        # planted file does, which says nothing about an arbitrary one.
        #
        # Denied anyway, and each of these alone is sufficient: an ungated raw host path, a
        # readability oracle over the whole filesystem, and full disclosure from a planted file
        # (`parse_upstart.py:55` `link.name = fields[2]`; `:66,:68` `device.name`), read back
        # through `list_entities`.
        #
        # **No hardware precondition.** An earlier reading of this said "reachable only with UPB
        # hardware attached" — `connection.py:58-99` retries forever, so `config_flow.py:45` looked
        # unreachable without a PIM. That is false, and the whole chain is caller-controlled:
        # `CONF_DEVICE` is a `SerialPortSelector`, which does **not** validate that the value names
        # a device, and `upb_lib` hands it to `serialx`, which honours the pyserial URL family. A
        # `socket://host:port` pointing at any listening TCP port satisfies `connect()` — the box's
        # own `:8123`, anything on its LAN. So `upb` is reachable with `integrations.manage` alone:
        # the `downloader` class, not a niche one.
        #
        # Re-runnable in two commands rather than re-derived (`uv run --with homeassistant
        # --with upb_lib`):
        #   SerialPortSelector({})("socket://127.0.0.1:8123")   -> returned unchanged, no error
        #   UpbPim({"url": "socket://127.0.0.1:<any listener>"}).async_connect()
        #                                                       -> returns, is_connected() True
        # Verified on the **async** path `upb_lib` actually calls (`open_serial_connection`), not
        # the sync `serial_for_url` helper. Note what that run did and did not show: it used a
        # crafted UPE file, so it establishes reachability and planted-file disclosure, and it is
        # the run that was over-read into the severity claim corrected above.
        #
        # Invisible to `test_every_path_writing_service_is_classified`, whose scan globs
        # `*/services.yaml`: upb declares services, but none of them takes a path. The danger is
        # entirely in the config flow. Established by reading `upb_lib` rather than assuming a
        # bound — see `RESEARCH/HEARTH_UPB_SETUP_AXIS/FINDINGS.md`.
        "upb",
    }
)

# Field names that hold credentials even when the schema does not say so: older flows use a bare
# string for passwords, and newer ones for BLE keys, alarm codes and PSKs (#668). Pinned equal to
# `packages/shared/src/secrets.ts` by `test_allowlist.py`, with the hub's verdict for every shared
# case, because the hub refuses the same input and its audit redactor masks the same keys. The first
# eleven substrings are the matcher this replaced, so nothing it refused is allowed now; the token
# rules are anchored to whole tokens, because a bare `key` or `code` substring refuses `key_id` and
# `country_code`, and `NOT_SECRET_NAMES` (each read in HA 2026.9.3) is exempt from those rules only.
SECRET_NAME_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "credential",
    "client_secret",
    "pin",
    "passcode",
    "access_key",
    "passphrase",
    "passkey",
    "bindkey",
)
SECRET_NAME_TOKENS: frozenset[str] = frozenset(
    {"key", "keys", "psk", "otp", "totp", "hotp", "cookie", "cookies", "adbkey", "privkey", "pincode", "usercode", "usercodes", "pwd", "pswd"}
)
SECRET_NAME_FINAL_TOKENS: frozenset[str] = frozenset({"code"})
SECRET_NAMES: frozenset[str] = frozenset({"device_authentication"})
NOT_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "key_id",
        "country_code",
        "zip_code",
        "post_code",
        "postal_code",
        "place_code",
        "station_code",
        "language_code",
        "qr_code",
        "event_code",
        "mac_code",
        "request_new_code",
        "use_psk",
        "code_format",
    }
)
# A form is a few levels deep at most: a section, or an action's `data`.
SECRET_WALK_MAX_DEPTH = 32

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_NOT_TOKEN = re.compile(r"[^a-z0-9]+")


def _folded(name: str) -> str:
    # `str.lower` leaves the long s alone; JavaScript's `toLowerCase` does too, and the old
    # `re.IGNORECASE` matcher folded it, so both sides fold it by hand.
    return name.lower().replace("ſ", "s")


def secret_name_tokens(name: str) -> list[str]:
    """The name's tokens: split at a lower-to-upper change, then on anything not a letter or digit."""
    return [t for t in _NOT_TOKEN.split(_folded(_CAMEL.sub(r"\1_\2", name))) if t]


def is_secret_name(name: str) -> bool:
    """True when a field of this name holds a credential Hearth must not send (`isSecretFieldName`)."""
    lower = _folded(name)
    if any(s in lower for s in SECRET_NAME_SUBSTRINGS):
        return True
    tokens = secret_name_tokens(name)
    joined = "_".join(tokens)
    if joined in NOT_SECRET_NAMES:
        return False
    if joined in SECRET_NAMES:
        return True
    if any(t in SECRET_NAME_TOKENS for t in tokens):
        return True
    return bool(tokens) and tokens[-1] in SECRET_NAME_FINAL_TOKENS


def find_secret_paths(value: Any) -> list[str]:
    """Every path in a form's values whose key names a credential, sorted (`findSecretFieldPaths`).

    A section arrives as a nested dict (`{"auth": {"password": ...}}`), which a top-level read missed
    (#668). A path names keys and list indices, never a value. A value nested deeper than
    `SECRET_WALK_MAX_DEPTH` is refused at the path where the walk stopped: no form is that deep, and
    what the walk cannot see it cannot call safe.
    """
    out: list[str] = []

    def walk(v: Any, path: list[str], depth: int) -> None:
        if depth > SECRET_WALK_MAX_DEPTH:
            out.append(".".join(path))
            return
        if isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, [*path, str(i)], depth + 1)
        elif isinstance(v, dict):
            for k, x in v.items():
                if is_secret_name(str(k)):
                    out.append(".".join([*path, str(k)]))
                else:
                    walk(x, [*path, str(k)], depth + 1)

    walk(value, [], 0)
    return sorted(out)


def _dict_paths(value: Any, prefix: str = "") -> set[str]:
    """Every key path through nested dicts: where a section's fields land in a submission."""
    out: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}{k}"
            out.add(path)
            out |= _dict_paths(v, f"{path}.")
    return out


def secret_paths(fields: list[dict[str, Any]], prefix: str = "") -> list[str]:
    """The secret fields of a serialised form, a section's own as `section.field`."""
    out: list[str] = []
    for f in fields:
        path = f"{prefix}{f.get('name', '')}"
        inner = f.get("fields")
        if is_secret_name(str(f.get("name", ""))):
            out.append(path)
        elif isinstance(inner, list) and inner:
            out += secret_paths(inner, f"{path}.")
        elif f.get("secret"):
            out.append(path)
    return out

# What we showed the model for each open flow: {flow_id: [field, ...]}. Bounded, because a flow the
# model abandons without telling us would otherwise leak an entry apiece.
DATA_FLOW_FIELDS = f"{DOMAIN}_flow_fields"
MAX_TRACKED_FLOWS = 32

# Serialising a flow's form: Home Assistant 2026+ uses probatio, older releases voluptuous-serialize.
try:  # pragma: no cover - depends on the HA version at runtime
    from probatio import Schema as SchemaType, to_field_list as serialize_schema
except ImportError:  # pragma: no cover
    from voluptuous import Schema as SchemaType
    from voluptuous_serialize import convert as serialize_schema


def _section_fields(field: dict[str, Any]) -> list[dict[str, Any]]:
    """A `section`'s own fields: the serialiser shows one as a single `expandable` field (#668)."""
    inner = field.get("schema")
    return [f for f in inner if isinstance(f, dict)] if isinstance(inner, list) else []


def is_secret(field: dict[str, Any]) -> bool:
    """A credential by its name, by a password-typed selector, or a section holding either."""
    name = str(field.get("name", ""))
    if is_secret_name(name):
        return True
    selector = field.get("selector")
    if isinstance(selector, dict):
        text = selector.get("text")
        if isinstance(text, dict) and text.get("type") == "password":
            return True
    return any(is_secret(f) for f in _section_fields(field))


def flatten(field: dict[str, Any]) -> dict[str, Any]:
    """Home Assistant's serialisation -> the compact shape the hub's schema expects.

    A secret field goes out without its `default` or `description` (#668). A reauth form is shown
    with `default=entry.data[CONF_PASSWORD]`, and `description` carries `suggested_value`, so the
    stored password came back to Hearth beside `secret: true` without anyone typing it. A section's
    fields are kept under `fields`, so `check_secrets` knows which of them were secret.
    """
    selector = field.get("selector") if isinstance(field.get("selector"), dict) else None
    kind = field.get("type")
    if not kind and selector:
        kind = next(iter(selector), "string")
    secret = is_secret(field)
    out: dict[str, Any] = {
        "name": str(field.get("name", "")),
        "type": str(kind or "string"),
        "required": bool(field.get("required", False)),
        "secret": secret,
    }
    if inner := _section_fields(field):
        out["fields"] = [flatten(f) for f in inner]
    if not secret and (default := field.get("default")) is not None:
        out["default"] = default
    if not secret and (desc := field.get("description")) is not None:
        out["description"] = str(desc)
    options = field.get("options")
    if options is None and selector:
        for cfg in selector.values():
            if isinstance(cfg, dict) and "options" in cfg:
                options = cfg["options"]
                break
    if isinstance(options, list):
        out["options"] = [o if isinstance(o, (str, dict)) else str(o) for o in options][:50]
    return out


def serialize_fields(schema: Any, what: str) -> list[dict[str, Any]]:
    """A form's fields, or an empty list: a form we cannot describe is still a form we can submit."""
    if schema is None:
        return []
    try:
        return [flatten(f) for f in serialize_schema(schema, custom_serializer=cv.custom_serializer)]
    except Exception:  # noqa: BLE001 - a form we cannot describe is still a valid form
        _LOGGER.warning("could not serialise the %s form", what)
        return []


def serialize_field_map(fields: Any, what: str) -> list[dict[str, Any]]:
    """Serialise a plain `{marker: validator}` mapping, one field at a time.

    Home Assistant's storage collections describe their forms as a dict of markers rather than as a
    built `Schema`, and some of their validators are plain functions the serialiser does not know —
    `cv.icon` is one. Serialising the mapping in one go means a single unknown validator loses the
    whole form, so each field is tried on its own and an unserialisable one falls back to its name.
    A field described only by its name is still a field a caller can fill in; a form of nothing is
    not.
    """
    out: list[dict[str, Any]] = []
    for marker, validator in fields.items():
        try:
            out += [flatten(f) for f in serialize_schema(SchemaType({marker: validator}), custom_serializer=cv.custom_serializer)]
        except Exception:  # noqa: BLE001 - an undescribable field is still a submittable one
            name = getattr(marker, "schema", marker)
            out.append(
                {
                    "name": str(name),
                    "type": "string",
                    "required": type(marker).__name__ == "Required",
                    "secret": is_secret_name(str(name)),
                }
            )
    return out


def remember_fields(hass: HomeAssistant, flow_id: str, fields: list[dict[str, Any]]) -> None:
    """Keep the serialisation we handed out, so `flow_step` can tell which fields were secret."""
    store: dict[str, list[dict[str, Any]]] = hass.data.setdefault(DATA_FLOW_FIELDS, {})
    store.pop(flow_id, None)
    while len(store) >= MAX_TRACKED_FLOWS:
        # Bounded, oldest first. It used to clear the whole store, so opening 32 flows was a way to
        # make Hearth forget which fields of a live form were password-typed (#668). A forgotten form
        # is read again from the flow manager by `check_secrets`, and refused if it cannot be.
        store.pop(next(iter(store)))
    store[flow_id] = fields


def forget_fields(hass: HomeAssistant, flow_id: str) -> None:
    (hass.data.get(DATA_FLOW_FIELDS) or {}).pop(flow_id, None)


def remembered_fields(hass: HomeAssistant, flow_id: str) -> list[dict[str, Any]]:
    return (hass.data.get(DATA_FLOW_FIELDS) or {}).get(flow_id, [])


def shape(hass: HomeAssistant, domain: str, result: dict[str, Any]) -> dict[str, Any]:
    """Turn a FlowResult into the wire shape, and remember which fields are secret."""
    out: dict[str, Any] = {"flow_id": str(result.get("flow_id", "")), "domain": domain, "type": str(result["type"].value)}
    rtype = result["type"]

    if rtype is FlowResultType.FORM:
        out["step_id"] = result.get("step_id")
        out["errors"] = {k: str(v) for k, v in (result.get("errors") or {}).items()}
        # A placeholder named like a credential is left out: `otp`'s confirm step puts the new TOTP
        # seed in one called `code` (#668).
        placeholders = result.get("description_placeholders") or {}
        out["description_placeholders"] = {k: str(v) for k, v in placeholders.items() if not is_secret_name(str(k))} or None
        fields = serialize_fields(result.get("data_schema"), f"{domain} step {result.get('step_id')}")
        out["fields"] = fields
        out["secret_fields"] = secret_paths(fields)
        remember_fields(hass, out["flow_id"], fields)
    elif rtype is FlowResultType.CREATE_ENTRY:
        out["title"] = result.get("title")
        entry = result.get("result")
        out["entry_id"] = getattr(entry, "entry_id", None)
        forget_fields(hass, out["flow_id"])
    elif rtype is FlowResultType.ABORT:
        out["reason"] = result.get("reason")
        forget_fields(hass, out["flow_id"])
    elif rtype is FlowResultType.MENU:
        out["step_id"] = result.get("step_id")
        options = result.get("menu_options") or []
        out["menu_options"] = list(options) if isinstance(options, (list, tuple)) else list(options.keys())
    return out


def check_domain(domain: str) -> str:
    if not re.fullmatch(r"[a-z0-9_]+", domain):
        raise RpcError("invalid_params", "domain must be a lowercase slug")
    if domain in DENIED_DOMAINS:
        raise RpcError("method_not_allowed", f"Hearth does not set up the {domain} integration")
    return domain


def check_secrets(hass: HomeAssistant, flow_id: str | None, user_input: dict[str, Any], *, waiting: bool = True) -> None:
    """Refuse to relay credentials; the person finishes those steps in Home Assistant itself.

    Two halves. The name test catches a bare string field in an older flow, at any depth, because a
    section's fields arrive nested (#668). The remembered serialisation catches a password-*typed*
    field with an innocent name, in a section too — which is what the `flow.get("data_schema")` arm
    was meant to do and never could, because that key is not in an `async_progress()` dict at all.
    `flow_id` is None where there is no form yet (a helper's `config`), and only names are checked.

    The form is read from the flow manager first (`_live_form`), because Hearth did not start every
    flow it may be asked to step: a discovered or reauth flow comes from `integrations.discovered`,
    and a person may have moved a flow on in Home Assistant since Hearth showed it. What Hearth showed
    is the fallback. With neither, a non-empty step is refused (#668): an adax `wifi_pswd` is a
    password-typed field that no name rule could have caught.

    The refusal names the field, never the value: it reaches the hub and `audit_log.error`.
    """
    refused = set(find_secret_paths(user_input))
    finish = (
        ". The setup is waiting in Home Assistant under Settings > Devices & services; finish it there."
        if waiting
        else ". Make this helper in Home Assistant under Settings > Devices & services > Helpers instead."
    )
    if flow_id is not None:
        fields = _live_form(hass, flow_id)
        if fields is not None:
            remember_fields(hass, flow_id, fields)
        elif flow_id in (hass.data.get(DATA_FLOW_FIELDS) or {}):
            fields = remembered_fields(hass, flow_id)
        elif user_input and not refused:
            raise RpcError("method_not_allowed", "Hearth cannot read the form this setup is showing, so it will not fill it in" + finish)
        refused |= set(secret_paths(fields or [])) & _dict_paths(user_input)
    if refused:
        raise RpcError(
            "method_not_allowed",
            "Hearth will not send credentials on your behalf: "
            + ", ".join(sorted(refused))
            + finish,
        )


def _live_form(hass: HomeAssistant, flow_id: str) -> list[dict[str, Any]] | None:
    """The form a flow is showing now, serialised, or None when it cannot be read.

    `FlowManager._progress` and `FlowHandler.cur_step` are private: `async_progress()` builds its
    dicts without the schema. `async_configure` reads the same `cur_step` to validate a step
    (`data_entry_flow.py`), so it is the form the input will meet. Anything unexpected is None, and
    `check_secrets` then falls back to what Hearth showed, or refuses.
    """
    try:
        flow = hass.config_entries.flow._progress.get(flow_id)  # noqa: SLF001 - see the docstring
        step = getattr(flow, "cur_step", None) if flow is not None else None
        if not isinstance(step, dict):
            return None
        if step.get("type") is FlowResultType.MENU:
            return []
        if step.get("type") is not FlowResultType.FORM:
            return None
        schema = step.get("data_schema")
        if schema is None:
            return []
        # Not `serialize_fields`, which answers [] for a form it cannot read: here that would mean
        # "nothing secret", and an unreadable form is the case this exists to refuse.
        return [flatten(f) for f in serialize_schema(schema, custom_serializer=cv.custom_serializer)]
    except Exception:  # noqa: BLE001 - a private API that moved is a form we cannot read
        _LOGGER.debug("could not read the live form of flow %s", flow_id, exc_info=True)
        return None


async def check_flow_input(hass: HomeAssistant, config: Any) -> None:
    """The action policy, applied to form values exactly as it is to an authored automation.

    See the module docstring for why a form field needs this at all. The hub refuses the same input
    before it leaves (`flowInputPolicyCheck` in methods.ts); neither side is the only guard. Registry
    ids are resolved first (#663), under any key, since an `EntitySelector` field can have any name.

    It also checks, one hop away, the scene a value activates and the script it starts (#721), as
    `find_config_violations` does for an automation. That half has no hub mirror: only the box can
    read what a scene or a script holds.
    """
    config, problems = resolve_registry_ids(hass, config, "input", any_key=True)
    # Resolved, and what did not resolve is refused above without repeating the value, which can be a
    # BLE key here; so the walker, which would name it, is told (#663).
    problems += find_policy_violations(config, "input", registry_ids_resolved=True) + find_reference_violations(config, "input")
    # Literal, the two above. A form value naming a switch a `switch_as_x` gate covers is a new way to
    # move it: a group over the relay, or a template switch whose action calls it (#643, ruling 6).
    problems += find_gate_violations(hass, config, "input", mode="form")
    # Which remote a `remote.send_command` or `learn_command` in a form value reaches (#568, F1), and which activity a remote starts (#765), as for an automation.
    problems += find_remote_violations(hass, config, "input")
    # And what a scene or script the value activates would do (#721), asked exactly as it is of an
    # authored automation, over the resolved copy so a scene named by its registry id is read too.
    problems += await find_nested_authoring_violations(hass, config)
    if problems:
        raise RpcError("validation_failed", "Hearth will not save that: " + "; ".join(problems[:10]))


def check_switch_as_x_source(hass: HomeAssistant, domain: str, user_input: dict[str, Any]) -> None:
    """A `switch_as_x` helper may wrap only a switch the model can see and no gated wrapper holds (#643).

    `helpers.create` and `integrations.flow_step` both reach this flow, so both call it before Home
    Assistant sees the input. It stops two things. A second wrapper on a lock's relay, as a `cover`
    an owner could then share without knowing what it drives. And the flow as a way to find out which
    switches exist but are kept from the model: `helpers.list` shows a lock wrapper with no source id,
    so a source the model cannot see gets the words a source that does not exist gets, whether it is
    a ghost, hidden, unexposed or the relay under a lock. Home Assistant itself accepted a ghost and
    made the wrapper, so this is also a new refusal: Hearth no longer wraps a switch the owner has not
    exposed. A siren's relay is hidden like a lock's (`OFF_LIMITS`, #605), so only a valve source the
    model can see gets `method_not_allowed`, as in `devices.call`.
    """
    if domain != SWITCH_AS_X or "entity_id" not in user_input:
        return
    value = user_input["entity_id"]
    if not isinstance(value, str):
        raise RpcError("invalid_params", "entity_id must be a string")
    ent_reg = er.async_get(hass)
    source = _source_of(ent_reg, value)
    gates = _gates(hass)
    if source is None or hass.states.get(source) is None or not visible(hass, source, ent_reg.async_get(source), gates):
        raise RpcError("not_found", f"unknown entity {value}")
    if (gate := gates.get(source)) is not None:
        raise RpcError("method_not_allowed", f"{value}: this drives a {gate}; Hearth does not wrap it again")
