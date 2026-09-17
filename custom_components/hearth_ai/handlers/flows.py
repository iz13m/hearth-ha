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
from homeassistant.helpers import config_validation as cv

from ..const import DOMAIN
from ..policy import find_policy_violations, find_reference_violations
from ..rpc import RpcError

_LOGGER = logging.getLogger(__name__)

# Integrations Hearth will not set up on the user's behalf: they grant host-level power, or
# they are Hearth itself.
DENIED_DOMAINS: frozenset[str] = frozenset(
    {"hearth_ai", "hassio", "backup", "homeassistant", "command_line", "shell_command", "python_script", "ffmpeg"}
)

# Field names that hold credentials even when the schema does not say so (older flows use a
# bare string for passwords). Matched case-insensitively against the field name.
SECRET_NAME = re.compile(r"password|passwd|token|api_key|apikey|secret|credential|client_secret|pin|passcode|access_key", re.I)

# What we showed the model for each open flow: {flow_id: [field, ...]}. Bounded, because a flow the
# model abandons without telling us would otherwise leak an entry apiece.
DATA_FLOW_FIELDS = f"{DOMAIN}_flow_fields"
MAX_TRACKED_FLOWS = 32

# Serialising a flow's form: Home Assistant 2026+ uses probatio, older releases voluptuous-serialize.
try:  # pragma: no cover - depends on the HA version at runtime
    from probatio import to_field_list as serialize_schema
except ImportError:  # pragma: no cover
    from voluptuous_serialize import convert as serialize_schema


def is_secret(field: dict[str, Any]) -> bool:
    name = str(field.get("name", ""))
    if SECRET_NAME.search(name):
        return True
    selector = field.get("selector")
    if isinstance(selector, dict):
        text = selector.get("text")
        if isinstance(text, dict) and text.get("type") == "password":
            return True
    return False


def flatten(field: dict[str, Any]) -> dict[str, Any]:
    """Home Assistant's serialisation -> the compact shape the hub's schema expects."""
    selector = field.get("selector") if isinstance(field.get("selector"), dict) else None
    kind = field.get("type")
    if not kind and selector:
        kind = next(iter(selector), "string")
    out: dict[str, Any] = {
        "name": str(field.get("name", "")),
        "type": str(kind or "string"),
        "required": bool(field.get("required", False)),
        "secret": is_secret(field),
    }
    if (default := field.get("default")) is not None:
        out["default"] = default
    if (desc := field.get("description")) is not None:
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


def remember_fields(hass: HomeAssistant, flow_id: str, fields: list[dict[str, Any]]) -> None:
    """Keep the serialisation we handed out, so `flow_step` can tell which fields were secret."""
    store: dict[str, list[dict[str, Any]]] = hass.data.setdefault(DATA_FLOW_FIELDS, {})
    if len(store) >= MAX_TRACKED_FLOWS and flow_id not in store:
        # Bounded rather than clever: forgetting a form costs only the selector-based half of the
        # credential check, and the name-based half still holds for every field.
        store.clear()
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
        out["description_placeholders"] = {k: str(v) for k, v in (result.get("description_placeholders") or {}).items()} or None
        fields = serialize_fields(result.get("data_schema"), f"{domain} step {result.get('step_id')}")
        out["fields"] = fields
        out["secret_fields"] = [f["name"] for f in fields if f["secret"]]
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


def check_secrets(hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]) -> None:
    """Refuse to relay credentials; the person finishes those steps in Home Assistant itself.

    Two halves. The name test catches a bare string field in an older flow. The remembered
    serialisation catches a password-*typed* field with an innocent name — which is what the
    `flow.get("data_schema")` arm was meant to do and never could, because that key is not in an
    `async_progress()` dict at all.
    """
    refused = {k for k in user_input if SECRET_NAME.search(str(k))}
    refused |= {str(f["name"]) for f in remembered_fields(hass, flow_id) if f.get("secret") and f.get("name") in user_input}
    if refused:
        raise RpcError(
            "method_not_allowed",
            "Hearth will not send credentials on your behalf: "
            + ", ".join(sorted(refused))
            + ". The setup is waiting in Home Assistant under Settings > Devices & services; finish it there.",
        )


def check_flow_input(config: Any) -> None:
    """The action policy, applied to form values exactly as it is to an authored automation.

    See the module docstring for why a form field needs this at all. The hub refuses the same input
    before it leaves (`flowInputPolicyCheck` in methods.ts); neither side is the only guard.
    """
    problems = find_policy_violations(config, "input") + find_reference_violations(config, "input")
    if problems:
        raise RpcError("validation_failed", "Hearth will not save that: " + "; ".join(problems[:10]))
