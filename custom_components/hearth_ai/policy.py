"""Action policy — mirror of packages/shared/src/policy.ts. Enforced before any config is written.

The allowlist stops the hub from calling services directly; this stops it from smuggling
denied services into automations/scripts/scenes that HA would then run on its behalf.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote

DENIED_ACTION_DOMAINS: frozenset[str] = frozenset(
    {
        "lock",
        "alarm_control_panel",
        "camera",
        "shell_command",
        "python_script",
        "hassio",
        "update",
        "device_tracker",
        "person",
        "ffmpeg",
        "stream",
        "backup",
        "recorder",
        "onboarding",
        "auth",
        "cloud",
        # A siren is the alarm panel's output: silencing one during an intrusion or a fire is the
        # audible half of a disarm, and sounding one can call out a monitoring service. Refused like
        # `alarm_control_panel`, ruled by the owner (#527). Some "sirens" are chimes; refusing them
        # too is the side to err on.
        "siren",
    }
)
DENIED_ACTIONS: frozenset[str] = frozenset(
    {
        "homeassistant.restart",
        "homeassistant.stop",
        "homeassistant.reload_all",
        "homeassistant.reload_core_config",
        "homeassistant.reload_config_entry",
        "homeassistant.set_location",
        "system_log.write",
        "logger.set_level",
        "persistent_notification.dismiss_all",
        # Hearth never triggers an automation, and never flips one on or off from inside a config it
        # authored. `automations.set_enabled` is the one sanctioned path and it is an RPC method
        # under `automations.write`, checked and audited as itself — not a line a model can bury in
        # a script, where turning an automation off is how you quietly disarm someone else's rule.
        # #227, the **call** axis. Denying `downloader` at setup only was half a fix: a household
        # whose download directory is already the config directory — #227's own demonstrated
        # configuration, reachable through the ordinary config flow — is still exposed, and the
        # caller chooses both the path and the *content* via the URL. Strictly worse than the
        # homematic clobber, which was denied here from the start.
        "downloader.download_file",
        # `raise_if_invalid_path(directory_path)` rejects `..` and **nothing else**: `/config` and
        # `/etc/cron.d` both pass it (checked). The only `is_allowed_path` in the component guards
        # `load_data`, the *outbound* send. `Path(custom_path).write_bytes(...)` at bot.py:1124 is
        # the destination and is ungated, so this writes caller-chosen bytes to a caller-chosen
        # host path with `automations.write` alone.
        "telegram_bot.download_file",
        # Redirects a `local_file` camera at any path `os.access(R_OK)` allows, and `camera_image`
        # then serves that file's bytes. A host-level *read* rather than a write, refused for the
        # same reason: invariant 1 does not distinguish. Only a literal `camera.*` target was
        # caught before; `device_id`, `area_id` and `entity_id: all` were not, because this is not
        # a fan-out action.
        "local_file.update_file_path",
        # Writes `Path(config_output_path)` with `write_text` and no `is_allowed_path`
        # (`homematicip_cloud/services.py:313,319`). It cannot clobber an arbitrary file: line 312
        # builds `f"{prefix}_{sgtin}.json"`, so every write ends `_<sgtin>.json` and `secrets.yaml`
        # is unreachable. It is denied because **both** inputs are unvalidated `cv.string` — the
        # prefix may carry `/` and `..`, and `config_output_path` is not checked at all — so the
        # directory is caller-chosen even though the basename is not. Fixed content
        # makes it a clobber rather than an injection, which changes the severity and not the
        # classification: invariant 1 refuses host-level services everywhere, and a service that
        # writes to a caller-chosen host path is host-level in effect. Service-level rather than
        # domain-level on purpose — denying the domain would break legitimate Homematic control.
        # Reachable with `automations.write`, which is **on by default** (#227).
        "homematicip_cloud.dump_hap_config",
        "automation.trigger",
        "automation.turn_on",
        "automation.turn_off",
        "automation.toggle",
        # A remote plays whatever the household taught it, and an RF-capable one (Broadlink's RM Pro)
        # can learn an alarm keyfob, a siren or a lock: a device `DENIED_ENTITY_DOMAINS` refuses by
        # domain that HA never sees as one. `send_command` plays a learned code by name or any raw
        # `b64:` code; `learn_command` adds one; `delete_command` erases codes only the physical remote
        # can teach again. On/off/toggle stay allowed (#568). Refused for every Hearth caller, not
        # only the model: neither the app nor voice offers these.
        "remote.send_command",
        "remote.learn_command",
        "remote.delete_command",
        # Hands a free-form command string to the vendor library unchanged. On Roborock that reaches
        # the camera (`set_camera_status`, `start_camera_preview`), remote driving and Wi-Fi
        # deletion; on python-miio, `miIO.ota` installs firmware from a URL the caller picks. A
        # camera reached through another domain's service. Service-level, ruled by the owner (#554):
        # the named vacuum services (`start`, `return_to_base`, `clean_area`, …) stay allowed.
        "vacuum.send_command",
        # Puts a whole Lovelace dashboard on a Cast device (#536, found by Security). The receiver
        # signs in as the Cast system user, which `cast/home_assistant_cast.py` creates in the admin
        # group, so the television renders whatever the view holds, camera cards and lock tiles
        # included. Nothing in the call names a camera for the reference walk to catch.
        "cast.show_lovelace_view",
        # #536, option A (the owner's ruling): a model may not start, browse or search media. Each
        # takes a free-text media id, and `media-source://camera/<id>` resolves through the camera's
        # own media source and comes back signed, so a television shows the camera
        # (`test_media_camera.py`). `_CAMERA_MEDIA_RE` stays as a backstop on every other field; a
        # template assembles an id after every check has run. A typed tool may come later.
        "media_player.play_media",
        "media_player.browse_media",
        "media_player.search_media",
        "media_extractor.play_media",
        "music_assistant.play_media",
        "music_assistant.play_announcement",
        # Services that send media or an attachment off the box, or onto a screen; a template
        # assembles `media-source://camera/<id>` after every check has run. `ai_task`'s two services
        # and `openai_conversation.generate_content` hand a frame to the LLM provider.
        # `mastodon.update_profile`, `ntfy.publish` and `smtp.send_message` special-case a camera or
        # image media id, call `camera.async_get_image` with no exposure check and send the still out.
        # `opendisplay.upload_image` draws any media id on an e-ink display, and
        # `media_extractor.extract_media_url` fetches any URL. Mirror of the list in policy.ts.
        "ai_task.generate_data",
        "ai_task.generate_image",
        "openai_conversation.generate_content",
        "mastodon.update_profile",
        "ntfy.publish",
        "smtp.send_message",
        "opendisplay.upload_image",
        "media_extractor.extract_media_url",
        # #581 option A: services that address a device by Z-Wave node, Zigbee IEEE address, MQTT
        # topic or raw 433 MHz frame, so they never name the `lock.*` entity HA exposes for the same
        # device and the domain lists cannot see them. Each can unlock a door. `zwave_js.set_value`
        # resolves any entity on a device to its node, so a lock's door `binary_sensor` (exposed by
        # default) is enough even on the control path; `mqtt.publish` writes Zigbee2MQTT's
        # `<lock>/set` topic; the ZHA command sends the door-lock cluster (257) directly.
        # **A lower bound, not the class.** These are the services measured from a name sweep of
        # `services.yaml`; the next Home Assistant release can add one this list does not name and no
        # test goes red. The structural fix, an allowlist of the domains a model may call, is #581's
        # open option B.
        "zwave_js.set_value",
        "zwave_js.multicast_set_value",
        "zwave_js.invoke_cc_api",
        "mqtt.publish",
        "zha.issue_zigbee_cluster_command",
        "rfxtrx.send",
        "rflink.send_command",
        # #581 option A, found by Code Reviewer: the same integrations' twins of the entries above,
        # reaching the lock the same way. ZHA's attribute write sets door-lock attributes such as
        # OperatingMode and AutoRelockTime by IEEE address; its group command sends the cluster
        # command to a group. The Z-Wave config-parameter services resolve a lock's door sensor to its
        # node, as `set_value` does. The cost is the siblings' cost: a household automation setting a
        # parameter on a device that is not a lock is refused too.
        "zha.set_zigbee_cluster_attribute",
        "zha.issue_zigbee_group_command",
        "zwave_js.set_config_parameter",
        "zwave_js.bulk_set_partial_config_parameters",
        # #581 option A, found by Security from #581 option B's draft: services Home Assistant
        # registers on the `lock` platform itself (`entity_domain=LOCK_DOMAIN`, or an entity service
        # registered in an integration's `lock.py`). Named by a `lock.*` entity
        # they are already refused by the entity-domain check, but HA resolves a `device_id`,
        # `area_id`, `floor_id` or `label_id` target, or `entity_id: all`, to the locks behind it
        # (`helpers/service.py`, `referenced | indirectly_referenced`), and none of those names a
        # `lock.*` entity. So they are refused by name, whatever the target. Three groups: writing a
        # code or user (a new way in), opening or changing how the lock locks, and reading codes or
        # users back as a service response. Core integrations only, so a lower bound like the list
        # above.
        "zwave_js.set_lock_usercode",
        "zwave_js.clear_lock_usercode",
        "zwave_js.set_user",
        "zwave_js.delete_user",
        "zwave_js.delete_all_users",
        "zwave_js.set_credential",
        "zwave_js.delete_credential",
        "zwave_js.delete_all_credentials",
        "zwave_js.set_lock_configuration",
        "zha.set_lock_user_code",
        "zha.clear_lock_user_code",
        "zha.enable_lock_user_code",
        "zha.disable_lock_user_code",
        "matter.set_lock_user",
        "matter.clear_lock_user",
        "matter.set_lock_credential",
        "matter.clear_lock_credential",
        "isy994.set_zwave_lock_user_code",
        "isy994.delete_zwave_lock_user_code",
        "schlage.add_code",
        "schlage.delete_code",
        "nuki.lock_n_go",
        "nuki.set_continuous_mode",
        "subaru.unlock_specific_door",
        "verisure.enable_autolock",
        "verisure.disable_autolock",
        "zwave_js.get_lock_usercode",
        "zwave_js.get_users",
        "zwave_js.get_credential_capabilities",
        "matter.get_lock_info",
        "matter.get_lock_users",
        "matter.get_lock_credential_status",
        "schlage.get_codes",
        # #662, the #581 option-A row for a siren: ZHA sounds a warning device by its Zigbee IEEE
        # address, so these name no `siren.*` entity, while ZHA exposes the same device as `siren.*`.
        # ZHA's `warn` and `squawk` device actions call them too, through `DEVICE_ACTION_SERVICES`.
        "zha.warning_device_squawk",
        "zha.warning_device_warn",
        # #581 option A, the rest of the issue's sweep (HA 2026.9.3 `services.yaml`): each writes to a
        # device by bus address, register, group address, raw frame or gateway path, so it names no
        # lock, alarm or siren entity. ESPHome's per-device actions cannot be listed; option B's case.
        "deconz.configure",
        "modbus.write_register",
        "modbus.write_coil",
        "knx.send",
        "isy994.send_raw_node_command",
        "isy994.set_zwave_parameter",
        "pilight.send",
        "xiaomi_miio.remote_learn_command",
        # Same class: Homematic writes a datapoint or a whole paramset by bus address and channel,
        # so a Homematic lock is reached without naming its `lock.*` entity.
        "homematic.set_device_value",
        "homematic.put_paramset",
        # Found by Security on this branch: ElkM1's alarm and zone services (aimed at a device or an
        # area they name no `alarm_control_panel.*` entity), LCN's raw PCK, outputs, relays and keys
        # by module address, and Insteon's all-link groups, which can include a lock.
        "elkm1.alarm_bypass",
        "elkm1.alarm_clear_bypass",
        "elkm1.alarm_arm_home_instant",
        "elkm1.alarm_arm_night_instant",
        "elkm1.alarm_arm_vacation",
        "elkm1.alarm_display_message",
        "elkm1.sensor_zone_bypass",
        "elkm1.sensor_zone_trigger",
        "lcn.pck",
        "lcn.relays",
        "lcn.output_abs",
        "lcn.output_rel",
        "lcn.output_toggle",
        "lcn.send_keys",
        "lcn.lock_keys",
        "insteon.add_all_link",
        "insteon.delete_all_link",
        "insteon.load_all_link_database",
        "insteon.scene_on",
        "insteon.scene_off",
        # #672: `zha.remove {ieee}` takes a siren or an alarm's contact sensor off the network by IEEE
        # address alone; `zha.permit` opens the network to any device joining.
        "zha.remove",
        "zha.permit",
        # #643, ruling 9: an old-style group's members, changed while it runs. `group.set` can add a
        # lock's relay to a group an automation already turns on, and Hearth reads a group's gate off
        # its members only when it is asked. Not reachable through `devices.call` on the pinned
        # 2026.9.3 (its schema takes `object_id` and no `entity_id`); refused for a written config too,
        # because changing what a group holds is not something a model does. `reload` re-reads the
        # YAML groups, which undoes nothing a model should be able to undo either.
        "group.set",
        "group.remove",
        "group.reload",
        # #731: a model sends no media off the box. `immich.upload_file` uploads a local media item
        # (stills an owner's automation saved) to the Immich server; `google_photos.upload` uploads
        # files within the allowed paths to Google Photos, and `onedrive.upload` to a OneDrive folder.
        "immich.upload_file",
        "google_photos.upload",
        "onedrive.upload",
        # And `onedrive.delete`: a model has no reason to delete files, and OneDrive is also a backup
        # agent, so it can wipe the household's off-box backups.
        "onedrive.delete",
    }
)
# A device action whose `type` calls a service by another name (#662), keyed by folded `domain.type`.
# Mirror of `DEVICE_ACTION_SERVICES` in policy.ts, pinned by `test_lists_match_shared`. zwave_js needs
# no entry: its action types are its service names. ZHA maps `squawk` and `warn` to the two
# `warning_device_*` services (`zha/device_action.py`, `SERVICE_NAMES`) and calls them by IEEE address.
DEVICE_ACTION_SERVICES: Mapping[str, str] = MappingProxyType(
    {
        "zha.squawk": "zha.warning_device_squawk",
        "zha.warn": "zha.warning_device_warn",
    }
)
# A device action refused by its own folded `domain.type`, because it calls no service (#662). Mirror
# of `DENIED_DEVICE_ACTIONS` in policy.ts, pinned by `test_lists_match_shared`. RFXtrx's two action
# types call `getattr(device, type)` (`rfxtrx/device_action.py`), the same method its `siren.*` entity
# sounds with: `RfxtrxChime` through `send_command`, `RfxtrxSecurityPanic` through
# `send_status("Panic")` (`rfxtrx/siren.py`). Both types, so every RFXtrx device action is refused;
# its device triggers (`command`, `status`) are not.
DENIED_DEVICE_ACTIONS: frozenset[str] = frozenset({"rfxtrx.send_command", "rfxtrx.send_status"})
DENIED_ENTITY_DOMAINS: frozenset[str] = frozenset({"lock", "alarm_control_panel", "camera", "device_tracker", "person", "siren"})

# Valves move one way only from Hearth: closed (#527). Mirror of the valve rule in policy.ts, whose
# comment says why it is a rule about direction rather than a domain entry: closing must stay one
# tap, and `DENIED_ENTITY_DOMAINS` would hide every valve from the model and stop it closing one.
# `stop_valve` is left out: it cannot open a valve, but an automation triggered on `to: closing` that
# stops it leaves every later close halfway, the household's leak response included.
# An allowlist, so a valve service Home Assistant adds later starts refused. Scene states and device
# actions do nothing on the pinned 2026.9.3 (`valve` has no `reproduce_state` or `device_action`
# platform) and are refused anyway, for the version that adds them.
VALVE_DOMAIN = "valve"
_VALVE_CLOSING_SERVICES = ("close_valve",)
_VALVE_POSITION_SERVICE = "set_valve_position"
_VALVE_CLOSING_RULE = "Hearth only closes a valve (close_valve, or set_valve_position to a literal 0)"


def _is_literal_zero(value: Any) -> bool:
    """A number that is exactly zero. Not `"0"`, not `False` (which Python also calls 0)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _valve_service_closes(action: str, positions: list[Any] | None) -> bool:
    """Whether a call to `action` can only close a valve. Mirror of valveServiceCloses in policy.ts."""
    service = _fold(action)[len(VALVE_DOMAIN) + 1 :]
    if service in _VALVE_CLOSING_SERVICES:
        return True
    return service == _VALVE_POSITION_SERVICE and positions is not None and len(positions) > 0 and all(_is_literal_zero(p) for p in positions)


def _valve_state_closes(value: Any) -> bool:
    """A scene's stored valve state that can only leave it closed. Mirror of valveStateCloses."""
    if value == "closed" and isinstance(value, str):
        return True
    if not isinstance(value, dict):
        return False
    return value.get("state") == "closed" and all(k == "current_position" and _is_literal_zero(v) for k, v in value.items() if k != "state")


def _scene_valve_ids(node: dict[str, Any]) -> list[str]:
    """Every valve a scene service sets to something other than closed, or snapshots. Mirror of sceneValveIds."""
    out: list[str] = []

    def flat(value: Any) -> list[Any]:
        return value if isinstance(value, list) else [value]

    for src in (node, node.get("data"), node.get("data_template")):
        if not isinstance(src, dict):
            continue
        entities = src.get("entities")
        if isinstance(entities, dict):
            out.extend(str(k) for k, v in entities.items() if _domain(str(k)) == VALVE_DOMAIN and not _valve_state_closes(v))
        else:
            out.extend(x for x in flat(entities) if _domain(x) == VALVE_DOMAIN)
        # Comma-split like a target's `entity_id`: `cv.entity_ids` splits `"light.a, valve.main"`.
        out.extend(x for x in _entity_ids({"entity_id": src.get("snapshot_entities")}) if _domain(x) == VALVE_DOMAIN)
    return out


def _positions_of(node: dict[str, Any]) -> list[Any] | None:
    """The `position` values a written service call carries, or None when a carrier is a template."""
    out: list[Any] = []
    for key in ("data", "data_template"):
        value = node.get(key)
        if value is None:
            continue
        # A templated key is rendered too, and the later of two equal keys wins, so
        # `{position: 0, "{{ 'position' }}": 100}` opens the valve to 100.
        if not isinstance(value, dict) or _has_templated_key(value):
            return None
        if "position" in value:
            out.append(value["position"])
    return out

# Domains handled by their own capability, so device control never implicitly grants the
# ability to run user-authored sequences.
ROUTINE_DOMAINS: frozenset[str] = frozenset({"scene", "script", "automation"})


TARGET_KEYS: frozenset[str] = frozenset({"entity_id", "device_id", "area_id", "floor_id", "label_id", "target"})
"""Keys that would widen a call past the entity_ids it names; `devices.call` strips them from `data`.
Mirror of TARGET_KEYS in policy.ts."""


def find_service_call_violations(domain: str, service: str, entity_ids: list[str], data: Any = None) -> list[str]:
    """Mirror of findServiceCallViolations in packages/shared/src/policy.ts.

    `data` is read for a valve's position (#527), so leaving it out refuses `set_valve_position`, and
    walked whole by `find_reference_violations` (#536), because `media_player.play_media` puts whatever
    its `media_content_id` names on a television, a camera included.
    """
    import re  # noqa: PLC0415

    problems: list[str] = []
    # `full` keeps the caller's spelling, because it is what the messages quote back. Every
    # *comparison* uses the folded pair — mixing the two is how mixed case walked past
    # `DENIED_ACTIONS` here while the hub refused it: before the fold existed, the malformed-name
    # guard below rejected `Homeassistant.Restart` outright, so folding made that guard pass and
    # left a raw string being tested against a lower-case list.
    full = f"{domain}.{service}"
    raw_domain = domain
    domain, service = _fold(domain), _fold(service)
    folded = f"{domain}.{service}"
    if not re.fullmatch(r"[a-z0-9_]+", domain) or not re.fullmatch(r"[a-z0-9_]+", service):
        return [f"{full}: malformed service name"]
    if domain in DENIED_ACTION_DOMAINS:
        problems.append(f"{full}: domain {raw_domain} can never be controlled")
    if folded in DENIED_ACTIONS:
        problems.append(f"{full}: not allowed")
    if domain in ROUTINE_DOMAINS:
        problems.append(f"{full}: use activate_scene or run_script instead of calling the {raw_domain} domain directly")
    # Keyed on the service, not the ids: `valve.open_valve` reaches valves whatever it names (#527).
    positions = [data["position"]] if isinstance(data, dict) and "position" in data else []
    if domain == VALVE_DOMAIN and not _valve_service_closes(full, positions):
        problems.append(f"{full}: {_VALVE_CLOSING_RULE}")
    if not entity_ids:
        problems.append(f"{full}: at least one entity_id is required (broad targets are not allowed)")
    for eid in entity_ids:
        d = _domain(eid)
        if not d:
            problems.append(f"{eid}: malformed entity_id")
        # The referenceable list, not only the action list: `devices.call` answers with each target's
        # state, so an id the read path hides would come back through a call on it (#566).
        elif d in REFERENCEABLE_DENIED_DOMAINS:
            problems.append(f"{eid}: entities in {d} can never be controlled")
        elif d in ROUTINE_DOMAINS:
            problems.append(f"{eid}: use activate_scene or run_script for {d} entities")
        # `homeassistant.toggle` forwards to `valve.toggle`; no other domain's service may move a valve.
        elif d == VALVE_DOMAIN and domain != VALVE_DOMAIN:
            problems.append(f"{eid}: a valve is moved only by its own services; {_VALVE_CLOSING_RULE}")
    # Every key and value that is sent, nested ones included: `play_media` also takes `media:
    # {media_content_id}`. A target key is left out because both sides strip it rather than refuse it.
    # Service data is a mapping or nothing; anything else is refused rather than walked as a string.
    if data is not None and not isinstance(data, dict):
        problems.append(f"{full}: data must be an object")
    elif data:
        problems.extend(find_reference_violations({k: v for k, v in data.items() if k not in TARGET_KEYS}, f"{full}: data"))
    return list(dict.fromkeys(problems))


_LIST_KEYS = {"actions", "action", "sequence", "then", "else", "default", "parallel", "choose", "options", "repeat", "if", "conditions"}


def _domain(value: Any) -> str | None:
    """The domain part of `domain.object_id`, **case-folded** (#220).

    Home Assistant decides case *after* we do: `cv.service` and `cv.entity_id` both `.lower()` their
    input (`helpers/config_validation.py`), so `Lock.Unlock` is stored raw, passes a walk that
    compares against lower-case lists, and runs as `lock.unlock`. Folded here rather than at each
    call site, so a future check cannot forget to do it.
    """
    if not isinstance(value, str) or "." not in value:
        return None
    return value.split(".", 1)[0].lower()




def _fold(value: Any) -> str:
    """A whole service name or entity id as Home Assistant will read it. See `_domain`."""
    return value.lower() if isinstance(value, str) else ""


def _entity_ids(target: Any) -> list[str]:
    """The entity ids a service-call node names, read as Home Assistant reads them (#228).

    **Comma-split**, because `cv.comp_entity_ids` is: on the pinned 2026.9.3,
    `comp_entity_ids("input_boolean.test, automation.security")` returns both ids. Taking the string
    whole meant the walk saw one harmless entity while HA acted on two.
    """
    if not isinstance(target, dict):
        return []
    v = target.get("entity_id")
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, list):
        return [x.strip() for x in v if isinstance(x, str)]
    return []


def _service_name(node: dict[str, Any]) -> Any:
    """How a service-call node names the service it runs, per `cv.SERVICE_SCHEMA` (#228).

    `action` and `service_template` are `vol.Exclusive` members of one `"service name"` group, and
    `has_at_least_one_key(CONF_ACTION, CONF_SERVICE_TEMPLATE)` requires one of them — so
    `service_template` is not a legacy alias to overlook, it is one of exactly two ways the schema
    allows a service to be named. `service` is renamed to `action` upstream by
    `_backward_compat_service_schema`, which is why it is read here and `service_template` survives
    beside it. We read one of the two, which is why the same instruction was refused spelled
    `action:` and ran spelled `service_template:`.
    """
    for key in ("action", "service", "service_template"):
        # `is not None`, not `in`: TypeScript's `??` skips a null and falls through to the next
        # spelling, so `{"action": null, "service_template": "lock.unlock"}` was refused on the hub
        # and allowed here. JSON can express that, so it is a real mirror break even though Home
        # Assistant rejects `None` at `cv.service` (#228 review).
        if node.get(key) is not None:
            return node[key]
    # `cv.ACTIONS_MAP` maps those three to `call_service` and `scene` to its own action *type*: a
    # `{scene: "scene.gate"}` node activates a scene while naming no service, so a check keyed on
    # the service name never sees it. Normalised so every rule treats the shorthand as what it is.
    if "scene" in node:
        return "scene.turn_on"
    return None


# Every key of a service-call node that can carry the entities it acts on, per `cv.SERVICE_SCHEMA`.
# `data_template` sits beside `data` in the schema and `template_complex` renders it the same way.
# `SCRIPT_ACTION_BASE_SCHEMA` contributes only `alias`, `note`, `continue_on_error` and `enabled` —
# enumerated, not assumed — and `response_variable`/`metadata` name no entities.
TARGET_BEARING_KEYS = ("target", "data", "data_template")


def _data_is_variables(action: str) -> bool:
    """Whether `data` on this action is service data or a script's **variables** (#228 review).

    Home Assistant has two paths: `script.turn_on` reads variables from
    `service.data.get(ATTR_VARIABLES)`, so a legacy `entity_id`/`device_id` in its `data` really is
    a target selector. The **per-script service** `script.<id>` passes `variables=service.data` —
    the whole mapping becomes the script's variables and selects nothing. So refusing
    `{action: "script.notify_phone", data: {device_id: "abc"}}` was over-refusal, and that is how
    every "notify this device" script is invoked.
    """
    dom = _domain(action)
    return dom == "script" and _fold(action).split(".", 1)[1] not in ("turn_on", "toggle", "reload", "turn_off")


def _reach_decided_by_data(action: str) -> bool:
    """Actions whose reach is decided by what is inside `data` rather than by the service name.

    `cv.SERVICE_SCHEMA` gives `data`/`data_template` **two** alternatives —
    `Any(template, All(dict, template_complex))` — and only the dict was read, so a whole-template
    `data` contributed nothing to any structural check. That let the target *selection* be
    laundered: `scene.apply` with `data="{{ {'entities': {'lo' ~ 'ck.front_door': ...}} }}"` set a
    lock, and no regex can catch the `area_id` form because an area name is not an entity id.
    """
    return fans_out(action) or _fold(action) in ("scene.apply", "scene.create")


def _scene_entity_ids(node: dict[str, Any]) -> list[str]:
    """The entities a `scene` service call sets or snapshots — mirror of sceneEntityIds in policy.ts.

    `scene.apply` takes `{entities: {"lock.front_door": {state: "unlocked"}}}` and `scene.create`
    takes `{snapshot_entities: ["lock.front_door"]}`. Neither puts an id where `_entity_ids` looks —
    one hides it in an object *key*, the other in a bare list — so a lock could ride into an
    automation as scene data and HA would apply it on the model's behalf.
    """
    out: list[str] = []

    def push(value: Any) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(x for x in value if isinstance(x, str))

    for src in (node, node.get("data"), node.get("data_template")):
        if not isinstance(src, dict):
            continue
        entities = src.get("entities")
        if isinstance(entities, dict):
            out.extend(str(k) for k in entities)
        else:
            push(entities)
        # Comma-split like a target's `entity_id`: `cv.entity_ids` splits `"light.a, lock.front_door"`.
        out.extend(_entity_ids({"entity_id": src.get("snapshot_entities")}))
    return out


SCENE_UNSETTABLE_DOMAINS: frozenset[str] = frozenset({"group"}) | ROUTINE_DOMAINS
"""Domains a scene service may not set or snapshot. Mirror of SCENE_UNSETTABLE_DOMAINS in policy.ts.

A `group.*`'s `reproduce_state` reproduces the group's state on every member, so
`{entities: {"group.doors": "unlocked"}}` calls `lock.unlock` on each lock in it, and `group.set` can
build that group from a template. An `automation.*`, `script.*` or `scene.*` key turns the routine on
or off, which `DENIED_ACTIONS` and the routine rules keep from an authored config.
"""


def _scene_unsettable_violations(ids: list[str], action: str, p: str) -> list[str]:
    return [
        f"{p}: action {action} sets {eid}; a scene service may not set or snapshot a group, automation, script or scene, since what that reaches cannot be checked here"
        for eid in ids
        if _domain(eid) in SCENE_UNSETTABLE_DOMAINS
    ]


def _scene_template_violations(node: dict[str, Any], action: str, p: str) -> list[str]:
    """A scene service whose entities are named by a template. Mirror of sceneTemplateViolations.

    `cv.template_complex` and `render_complex` render a mapping's **keys** as well as its values, and
    `scene.apply` validates each rendered key as an entity id. So `{"{{ 'lo' ~ 'ck.front_door' }}":
    "unlocked"}` unlocks the door while `_scene_entity_ids` reads the raw key, and an `entities` or
    `snapshot_entities` that is itself a template renders to whatever it likes. Refused as unknowable,
    like a templated target (AgDR-0042). A stored scene in `scenes.yaml` is not rendered, so
    `find_scene_policy_violations` needs none of this.
    """
    problems: list[str] = []
    for src in (node, node.get("data"), node.get("data_template")):
        if not isinstance(src, dict):
            continue
        if "entities" in src:
            entities = src["entities"]
            if not isinstance(entities, dict):
                problems.append(f"{p}: action {action} sets entities that are not a mapping of entity ids; what it would reach cannot be known until it runs")
            else:
                for key in entities:
                    if not isinstance(key, str) or _TEMPLATE_RE.search(key):
                        problems.append(f"{p}: action {action} sets {key}, which is a template; what it would reach cannot be known until it runs")
        if "snapshot_entities" in src:
            snap = src["snapshot_entities"]
            items = [snap] if isinstance(snap, str) else snap if isinstance(snap, list) else None
            if items is None or any(not isinstance(x, str) or _TEMPLATE_RE.search(x) for x in items):
                problems.append(f"{p}: action {action} snapshots entities named by a template; what it would reach cannot be known until it runs")
    return problems


REFERENCEABLE_DENIED_DOMAINS: frozenset[str] = DENIED_ENTITY_DOMAINS | {"image", "image_processing"}
"""Domains a model may never name anywhere — mirror of REFERENCEABLE_DENIED_DOMAINS in policy.ts.

`DENIED_ENTITY_DOMAINS` plus `image` and `image_processing`: the read path hides both while the
action list does not, and something that merely *reads* an entity is filtered by the read path's
union. Keeping the same union here makes "never listed, never readable" and "never referenceable" one
set; `OFF_LIMITS` in `handlers/registry.py` is the other mirror, and `test_policy.py` asserts the two
are equal, because adding a domain to `HIDDEN_DOMAINS` alone leaves a model able to name it in a
template and read it through an exposable sensor (#566). `find_service_call_violations` refuses
target ids on this list too, because `devices.call` answers with its targets' state.
"""

HOST_SERVICE_DOMAINS: frozenset[str] = frozenset({"shell_command", "python_script", "hassio", "backup"})
"""Service domains that are host-level wherever they appear, including inside a template string."""

# `IGNORECASE`, because this one reads raw text rather than a parsed id: `Lock.Front_Door` names the
# same entity HA will open, and a case-sensitive pattern is blind to it (#220).
_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_/\-])(" + "|".join(sorted(REFERENCEABLE_DENIED_DOMAINS | HOST_SERVICE_DOMAINS)) + r")\.[a-z0-9_]+",
    re.IGNORECASE,
)
CAMERA_MEDIA_SOURCES: tuple[str, ...] = ("camera", "image", "motioneye", "nest", "netatmo", "reolink", "unifiprotect")
"""Media-source platforms that serve a camera's picture or an `image` entity (#536). Mirror of policy.ts.

On the pinned 2026.9.3 a player resolves `media-source://camera/<entity_id>` through the camera's own
media source, which looks the entity up whether or not it is shared with Assist, and Home Assistant
then signs the URL, so a television fetches the camera with no credentials (`test_media_camera.py`).
"""

# A camera or image feed named as media, read after percent-decoding (#536): a media-source id on a
# platform above, Home Assistant's proxy views, and the vendor views that serve recordings by id.
# Mirror of CAMERA_MEDIA_RE in policy.ts, whose comment says why `_REFERENCE_RE` misses these (an id
# inside a path is skipped on purpose) and why this is a backstop and not a boundary.
_CAMERA_MEDIA_RE = re.compile(
    r"media-source://(?:" + "|".join(CAMERA_MEDIA_SOURCES) + r")(?![a-z0-9_])"
    r"|(?<![a-z0-9_])(?:camera|image)_proxy"
    r"|(?<![a-z0-9_])api/+(?:nest|unifiprotect)/",
    re.IGNORECASE,
)


def _percent_decoded(text: str) -> str:
    """`%XX` runs decoded as UTF-8 until nothing changes. Mirror of percentDecoded in policy.ts."""
    out = text
    for _ in range(8):
        nxt = unquote(out)
        if nxt == out:
            break
        out = nxt
    return out


# Home Assistant's `is_template_string`: `{{`, `{%` **or `{#`**. A Jinja comment renders to nothing,
# so `lo{# #}ck.unlock` is a template that renders to `lock.unlock` and names no denied domain raw.
_TEMPLATE_RE = re.compile(r"\{\{|\{%|\{#")


def _has_templated_key(value: Any) -> bool:
    """Whether a mapping has a key Home Assistant will render (`template_complex` renders keys).

    A key that is not a string counts: the second walk in `_validate` reads the config as Home
    Assistant validated it, where a templated key is already a `Template` object. JSON keys are
    always strings, so this cannot make the box disagree with `hasTemplatedKey` on a shared case.
    """
    return isinstance(value, dict) and any(not isinstance(k, str) or _TEMPLATE_RE.search(k) for k in value)


def find_reference_violations(value: Any, path: str = "input") -> list[str]:
    """Every place a value names an entity in a domain the model may never reach (AgDR-0038).

    Mirror of `findReferenceViolations` in policy.ts. `find_policy_violations` only inspects the
    target of a node that already carries an `action`, which is right for an automation and not
    enough for a config-flow form: a `trend` helper's whole configuration is
    `{"entity_id": "lock.front_door"}` and a template sensor's is `{{ states('lock.front_door') }}`.

    Complete for the helpers whose fields are entity pickers and numbers; a **backstop, not a
    boundary**, for anything Home Assistant renders as a template, since
    `{{ states('lo' ~ 'ck.front_door') }}` renders the same and no regex will see it.
    """
    problems: list[str] = []

    def scan(text: str, p: str) -> None:
        for m in _REFERENCE_RE.finditer(text):
            problems.append(f"{p}: {m.group(0)} is in a domain Hearth never lets a model reach")
        for t in dict.fromkeys((text, _percent_decoded(text))):
            for m in _CAMERA_MEDIA_RE.finditer(t):
                problems.append(f"{p}: {m.group(0)} is a camera or image feed Hearth never lets a model reach")

    def visit(node: Any, p: str) -> None:
        if isinstance(node, str):
            scan(node, p)
        elif isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
        elif isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                scan(key, f"{p}.{key}")
                visit(v, f"{p}.{key}")

    visit(value, path)
    return list(dict.fromkeys(problems))


def find_policy_violations(config: Any, path: str = "config", *, registry_ids_resolved: bool = False) -> list[str]:
    """Human-readable violations for an automation or script config ([] = allowed). Mirror of `findPolicyViolations`.

    `registry_ids_resolved` is the box's one departure from the hub's walk, and only a caller that has
    already resolved entity registry ids (`handlers/resolve.py`) may pass it (#663). Such a caller has
    its own rule for an id that did not resolve: authoring refuses it in the resolver, without repeating
    a value that can be a BLE key in a flow form, and the run-time check on a labelled script lets it
    run, because HA's call raises on it and so reaches nothing (`scripts.script_run_violations`).
    Without it a registry id in a fan-out target is refused, as on the hub, which has no registry to
    resolve one against; that is the walk `POLICY_WALK_CASES` pins.
    """
    problems: list[str] = []

    def visit(node: Any, p: str) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
            return
        if not isinstance(node, dict):
            return
        action = _service_name(node)
        if isinstance(action, str):
            # A templated action name is refused outright: the service it would call cannot be known
            # until it runs, and `_domain("{{ 'lock.unlock' }}")` is `{{ 'lock`, which matches nothing.
            if _TEMPLATE_RE.search(action):
                problems.append(f"{p}: action {action} is a template; the service it would call cannot be known until it runs")
            dom = _domain(action)
            if dom in DENIED_ACTION_DOMAINS:
                problems.append(f"{p}: action {action} targets denied domain {dom}")
            if _fold(action) in DENIED_ACTIONS:
                problems.append(f"{p}: action {action} is not allowed")
            ids = [i for k in TARGET_BEARING_KEYS for i in _entity_ids(node.get(k))] + _entity_ids(node) + _entity_ids({"entity_id": node.get("scene")})
            # A valve moves one way from here: closed (#527). The service decides, whatever it targets.
            if dom == VALVE_DOMAIN and not _valve_service_closes(action, _positions_of(node)):
                problems.append(f"{p}: action {action} is not a service that only closes a valve; {_VALVE_CLOSING_RULE}")
            for eid in ids:
                if _domain(eid) == VALVE_DOMAIN and dom != VALVE_DOMAIN:
                    problems.append(f"{p}: action {action} aimed at {eid} could open a valve; {_VALVE_CLOSING_RULE}")
            if dom == "scene":
                problems.extend(f"{p}: action {action} could leave {eid} open; a scene may only close a valve" for eid in _scene_valve_ids(node))
                problems.extend(_scene_template_violations(node, action, p))
                scene_ids = _scene_entity_ids(node)
                problems.extend(_scene_unsettable_violations(scene_ids, action, p))
                ids += scene_ids
            for eid in ids:
                if _domain(eid) in DENIED_ENTITY_DOMAINS:
                    problems.append(f"{p}: entity {eid} is in denied domain {_domain(eid)}")
            # An action that decides its own blast radius from its target (#220). After the
            # denied-domain sweep above, which still applies to whatever ids it does name.
            problems.extend(find_fan_out_violations(action, node, ids, p, registry_ids_resolved=registry_ids_resolved))
        # A device action names no entity and carries no service: `{device_id, domain: "lock",
        # type: "unlock"}` has no `action` key at all, so everything above skips it while HA still
        # opens the door. The check is therefore on the shape and sits outside the action branch.
        # It catches a device *trigger* and *condition* too, deliberately — the same three keys are
        # all three shapes, and a lock is something the model may not even read the state of.
        device_domain = node.get("domain")
        if isinstance(device_domain, str) and _fold(device_domain) in DENIED_ENTITY_DOMAINS and ("type" in node or "device_id" in node):
            problems.append(f"{p}: device in denied domain {device_domain}")
        # A device action can be a denied service under another name (#581). zwave_js lists
        # `set_value`, `set_config_parameter`, `set_lock_usercode` and `clear_lock_usercode` as action
        # types and calls `zwave_js.<type>` with the node's `device_id`, so `{device_id, domain:
        # "zwave_js", type: "set_value"}` is the `device_id` row of #581 with no `action` key for the
        # check above to read. Triggers and conditions are not exempted, as above, so a trigger or
        # condition whose `domain.type` spells a denied service is refused too; that fails closed.
        # ZHA's `warn` and `squawk` are not service names, so they are read through
        # `DEVICE_ACTION_SERVICES` first (#662). RFXtrx's call no service, so they are refused by
        # name through `DENIED_DEVICE_ACTIONS` (#662).
        device_type = node.get("type")
        if isinstance(device_domain, str) and isinstance(device_type, str):
            named = f"{_fold(device_domain)}.{_fold(device_type)}"
            if DEVICE_ACTION_SERVICES.get(named, named) in DENIED_ACTIONS or named in DENIED_DEVICE_ACTIONS:
                problems.append(f"{p}: device action {device_domain}.{device_type} is not allowed")
        # A valve device action may only close (#527). Not exempted for a trigger or condition key:
        # `determine_script_action` settles a node carrying two action keys by `ACTIONS_MAP` order.
        if isinstance(device_domain, str) and _fold(device_domain) == VALVE_DOMAIN and ("type" in node or "device_id" in node) and node.get("type") != "close":
            problems.append(f"{p}: device action on a valve could open it; only type close is allowed")
        for k, v in node.items():
            if k in _LIST_KEYS or isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}")

    visit(config, path)
    return list(dict.fromkeys(problems))



# Target keys Home Assistant resolves against its own registries, which we cannot read here.
# `scene.turn_on` has refused these since AgDR-0042; `fans_out` generalises that rule.
# A tuple, in policy.ts's declaration order: `test_policy.py` compares the two walkers' messages
# as ordered lists, so a target carrying two of these must produce them in the same order here.
INDIRECT_TARGET_KEYS = ("device_id", "area_id", "floor_id", "label_id")

# A group's membership lives in Home Assistant, so a `group.*` id is as unresolvable as an area.
_GROUP_DOMAIN = "group"

# An entity registry id (#663). Mirror of `REGISTRY_ID_RE` in policy.ts, whose comment says why a
# fan-out action may not be aimed at one: it carries no domain to check. Lower-case only, as HA's
# `_FAKE_UUID_4_HEX` (`helpers/config_validation.py`) is. The box resolves these before it walks an
# authored config (`handlers/resolve.py`), so on the box this rule sees only an id that did not resolve.
_REGISTRY_ID = re.compile(r"[0-9a-f]{32}")


def fans_out(action: str) -> bool:
    """Whether an action's effect is decided by something other than its own domain (#220).

    Mirror of `fansOut` in packages/shared/src/policy.ts; see that doc comment for how the list was
    derived from the pinned Home Assistant rather than written from memory.
    """
    dom = _domain(action)
    folded = _fold(action)
    if dom == "homeassistant":
        return folded[len(dom) + 1 :] in {"turn_on", "turn_off", "toggle"}
    if folded == "scene.turn_on":
        return True
    if dom == "script":
        return folded[len(dom) + 1 :] not in {"reload", "turn_off"}
    return False


def _target_selection(node: dict[str, Any], action: str) -> tuple[set[str], list[str], bool]:
    """**One view of how an action selects what it touches**, from every carrier key (#228).

    `target`, `data` and `data_template` are three spellings of target selection, and
    `cv.SERVICE_SCHEMA` declares each as `Any(template, dict)` — two shapes behind one name. The
    predicate had three call sites which then diverged: the templated form was refused on `target`
    and invisible on the other two, so the same input laundered through a different spelling walked
    past the rule. Normalise first, decide once; a fourth carrier key is then one line here.

    Returns (keys, ids, unknowable). The dict alternative contributes keys and ids; the template
    alternative contributes `unknowable`, which is what it is at authoring time (AgDR-0042).
    """
    keys: set[str] = set()
    ids: list[str] = []
    unknowable = False
    # For the per-script service `script.<id>`, `data` is the script's variables rather than a
    # target — see `_data_is_variables`. Its `target` still selects.
    carriers = ("target",) if _data_is_variables(action) else TARGET_BEARING_KEYS
    for key in carriers:
        value = node.get(key)
        # A templated key is rendered like a templated value (`template_complex`), so
        # `data: {"{{ 'area' ~ '_id' }}": "garden"}` selects an area no key here names.
        if isinstance(value, str) or _has_templated_key(value):
            unknowable = True
        if isinstance(value, dict):
            keys.update(str(k) for k in value)
            ids.extend(_entity_ids(value))
    keys.update(k for k in INDIRECT_TARGET_KEYS if k in node)
    ids.extend(_entity_ids(node))
    ids.extend(_entity_ids({"entity_id": node.get("scene")}))
    return keys, ids, unknowable


def find_fan_out_violations(action: str, node: dict[str, Any], ids: list[str], p: str, *, registry_ids_resolved: bool = False) -> list[str]:
    """What a fan-out action may not carry: a target we cannot resolve, and so cannot check.

    An entity registry id is one (#663), as on the hub; see `_REGISTRY_ID`. `registry_ids_resolved`
    is for a caller that has already resolved them (`handlers/resolve.py`) and has its own rule for
    one that did not: see `find_policy_violations`.

    A refusal rather than a resolution, deliberately. Neither side can resolve an `area_id` at
    authoring time to a set that stays true afterwards — someone re-labelling a camera would change
    it. Refusing what cannot be checked is the only answer that survives the config being written.

    Home Assistant registers the generic services with `extra=vol.ALLOW_EXTRA`, so `entity_id` is
    validated by `cv.entity_ids` (which is why `entity_id: all` dies there) while these four pass
    through untouched and are resolved later by `async_extract_referenced_entity_ids`. That
    asymmetry is exactly why this vector is open and that one is not.
    """
    problems: list[str] = []
    keys, _sel_ids, unknowable = _target_selection(node, action)
    # An unknowable selection is refused wherever the action's reach is decided by that selection
    # rather than bounded by its own service name: `fans_out` **plus** `scene.apply`/`scene.create`,
    # which are domain-bounded services whose data inlines entity states of any domain. Checked
    # against `fans_out` rather than assumed — the predicate is phrased "not bounded by its own
    # domain", which a scene service passes.
    if unknowable and _reach_decided_by_data(action):
        problems.append(
            f"{p}: action {action} decides what it touches from its target, and that target is a "
            "template; what it would reach cannot be known until it runs"
        )
    if not fans_out(action):
        return problems
    for key in INDIRECT_TARGET_KEYS:
        if key in keys:
            problems.append(f"{p}: action {action} decides what it touches from its target, so it may not use {key} — name the entities")
    for eid in ids:
        if _TEMPLATE_RE.search(eid):
            problems.append(
                f"{p}: action {action} decides what it touches from its target, and {eid} is a "
                "template; what it would reach cannot be known until it runs"
            )
            continue
        if not registry_ids_resolved and _REGISTRY_ID.fullmatch(eid):
            problems.append(
                f"{p}: action {action} decides what it touches from its target, and {eid} is an entity "
                "registry id, which names no domain to check; name the entity"
            )
            continue
        ed = _domain(eid)
        if ed == _GROUP_DOMAIN:
            problems.append(f"{p}: action {action} aimed at {eid} reaches whatever that group holds, which is not knowable here")
        if ed in ROUTINE_DOMAINS and _domain(action) == "homeassistant":
            article = "an" if ed == "automation" else "a"
            problems.append(f"{p}: action {action} aimed at {eid} runs {article} {ed}; use activate_scene or run_script")
    if not ids and not unknowable and _domain(action) == "homeassistant":
        problems.append(f"{p}: action {action} needs the entities it acts on named here")
    return problems


def find_scene_policy_violations(config: Any) -> list[str]:
    entities = config.get("entities") if isinstance(config, dict) else None
    if not isinstance(entities, dict):
        return []
    return [f"entities.{eid}: denied domain {_domain(eid)}" for eid in entities if _domain(eid) in DENIED_ENTITY_DOMAINS] + [
        f"entities.{eid}: a scene may only close a valve" for eid, v in entities.items() if _domain(eid) == VALVE_DOMAIN and not _valve_state_closes(v)
    ] + [
        # A group's `reproduce_state` sets every member, and a stored scene is reproduced the same way.
        f"entities.{eid}: a scene may not set a group; what its members are cannot be checked here" for eid in entities if _domain(eid) == _GROUP_DOMAIN
    ]


def find_scene_write_violations(config: Any) -> list[str]:
    """What a scene Hearth **writes** may not set, on top of `find_scene_policy_violations` (#713).

    Mirror of `findSceneWriteViolations` in policy.ts. An `automation.*` key is reproduced as
    `automation.turn_on` / `turn_off`, so a stored scene fired by an authored `scene.turn_on` would
    switch a household rule, which AgDR-0042 keeps to `automations.set_enabled`. `script.*` and
    `scene.*` go with it, as in `SCENE_UNSETTABLE_DOMAINS`.

    Write only, and deliberately not part of `find_scene_policy_violations`: that one also runs every
    time a scene is activated (`find_scene_run_violations`), and the owner's own scene that sets an
    automation is theirs to run. The authoring-time *link* to such a scene is refused by
    `find_nested_scene_violations(..., authoring=True)` instead.
    """
    entities = config.get("entities") if isinstance(config, dict) else None
    if not isinstance(entities, dict):
        return []
    return [
        f"entities.{eid}: a scene may not set an automation, script or scene; switching a routine is not something an authored config may do"
        for eid in entities
        if _domain(eid) in ROUTINE_DOMAINS
    ]
