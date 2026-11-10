"""Action policy — mirror of packages/shared/src/policy.ts. Enforced before any config is written.

The allowlist stops the hub from calling services directly; this stops it from smuggling
denied services into automations/scripts/scenes that HA would then run on its behalf.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, NoReturn
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
        # Every service under `androidtv` is host-level in effect, the same class as
        # `shell_command`: `adb_command` runs any `adb shell` line on the TV and writes its output
        # into the player's `adb_response` attribute, `download` pulls a TV file into an allowed
        # path such as `/config/www` (served unauthenticated at `/local/`), `upload` pushes one from
        # `/config/www` or `/media`, and `learn_sendevent` reads raw input events. Those four are
        # the whole domain on 2026.9.3; the TV is still driven through `media_player.*`.
        "androidtv",
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
        # Erases codes the household taught the remote, and only the physical remote can teach them
        # again (#568). Sending a code by name and learning one are allowed by the owner's ruling of
        # 2026-11-08, with raw codes still refused: see `_REMOTE_SEND_COMMAND` below. Refused for every
        # Hearth caller, not only the model: neither the app nor voice offers it.
        "remote.delete_command",
        # Hands a free-form command string to the vendor library unchanged. On Roborock that reaches
        # the camera (`set_camera_status`, `start_camera_preview`), remote driving and Wi-Fi
        # deletion; on python-miio, `miIO.ota` installs firmware from a URL the caller picks. A
        # camera reached through another domain's service. Service-level, ruled by the owner (#554):
        # the named vacuum services (`start`, `return_to_base`, `clean_area`, …) stay allowed.
        "vacuum.send_command",
        # #764: sends any SSAP request the caller names (`ssap://<command>`) with any payload to an LG
        # webOS TV and returns the TV's answer (`webostv/media_player.py` `async_command` ->
        # `aiowebostv` `request`, `SupportsResponse.OPTIONAL`). That is the TV's whole API: launching
        # any app, a doorbell or camera feed's included, and reading what the TV answers. The other
        # two webOS services stay: `button` writes one line to the pointer-input socket, which takes
        # only key presses and cursor moves, and `select_sound_output` calls one fixed endpoint.
        "webostv.command",
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
        # #779: services that hand free text to a voice assistant that acts on devices, so the action
        # is chosen after every check here has run and names no entity for the domain lists to see.
        # `conversation.process` runs the text through a conversation agent, and the built-in one
        # turns "unlock the front door" into `lock.unlock` on an Assist-exposed lock, and a valve's
        # HassTurnOn into `open_valve` (`intent/__init__.py`). `assist_satellite.start_conversation`
        # puts `extra_system_prompt` (or `start_message`) into an LLM agent's system prompt for the
        # next utterance. The two `send_text_command`s hand text to Google Assistant or Alexa, which
        # control whatever is linked to them. `conversation.reload`, `assist_satellite.announce` and
        # `ask_question` (stops at speech-to-text) stay. Mirror of the list in policy.ts.
        "conversation.process",
        "assist_satellite.start_conversation",
        "google_assistant_sdk.send_text_command",
        "alexa_devices.send_text_command",
        # #782: Google Assistant SDK's legacy notify service sends `broadcast {message}` through the
        # same `async_send_text_commands` as `send_text_command`. Discovery names it
        # `notify.google_assistant_sdk`; a YAML `notify:` entry can load it under any name, which
        # `handlers/notify.py` resolves from the platform. Mirror of the list in policy.ts.
        "notify.google_assistant_sdk",
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
        # `xiaomi_miio.remote_learn_command` was `remote.learn_command` under the integration's name; that
        # one is allowed since the owner's ruling of 2026-11-08 (#568), this one stays refused (AgDR-0072).
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

# **No assistant opens an entrance; closing one is allowed** (R3, #616, #608, #613; AgDR-0074). Mirror of
# the entrance rule in policy.ts, whose comment says what an entrance is and why the rule is #527's valve
# rule applied per entity. Only the box knows which ids are entrances (`handlers/entrances.py`), so every
# walker here takes the set and treats a missing one as "no entrances", which is what the hub walks with.
ENTRANCE_COVER_CLASSES: frozenset[str] = frozenset({"garage", "gate", "door"})
ENTRANCE_LABEL = "Hearth: entrance"
ENTRANCE_RULE = (
    "Azka never lets an assistant open an entrance (a garage, gate or door, or anything labelled Hearth: entrance); "
    "it may close one. Open it yourself in the Home Assistant app"
)
ENTRANCE_RULE_AR = (
    "لا يسمح أذكى لأي مساعد بفتح مدخل (باب كراج أو بوابة أو باب، أو أي شيء يحمل التسمية Hearth: entrance)، "
    "ويمكنه إغلاقه. افتحه بنفسك من تطبيق Home Assistant"
)
# Per entity domain: the services that only close, and the position service that closes at a literal 0.
_ENTRANCE_CLOSING: Mapping[str, tuple[tuple[str, ...], str | None]] = MappingProxyType(
    {"cover": (("close_cover",), "set_cover_position"), "valve": (("close_valve",), "set_valve_position")}
)
_ENTRANCE_CLOSING_DEFAULT: tuple[tuple[str, ...], str | None] = (("turn_off",), None)
_ENTRANCE_CLOSING_DEVICE_TYPES: Mapping[str, str] = MappingProxyType({"cover": "close", "valve": "close"})
# Device trigger and condition types, which read rather than move. Mirror of ENTRANCE_READ_DEVICE_TYPES.
_ENTRANCE_READ_DEVICE_TYPES: frozenset[str] = frozenset(
    {
        "opened", "closed", "opening", "closing", "position", "tilt_position",
        "is_open", "is_closed", "is_opening", "is_closing", "is_position", "is_tilt_position",
        "turned_on", "turned_off", "changed_states", "is_on", "is_off",
    }
)
_NO_ENTRANCES: frozenset[str] = frozenset()
# `script.*` services that do not start the script they are named after. Mirror of SCRIPT_NOT_STARTING.
_SCRIPT_NOT_STARTING: frozenset[str] = frozenset({"turn_on", "turn_off", "toggle", "reload"})


def is_entrance_problem(problem: str) -> bool:
    """Whether a refusal is the entrance rule's own: every one of its sentences carries one of these."""
    return ENTRANCE_RULE in problem or "with an entrance" in problem or "sets an entrance" in problem


def raise_refusal(code: str, problems: list[str], prefix: str = "") -> NoReturn:
    """Raise `problems` as one `RpcError`, tagged an entrance refusal when that is all they are (code review of e55c05e).

    Any other refusal wins: a scene that sets a lock is refused as that, in its words, whatever else it
    does. Only a refusal that is the entrance rule alone carries `ENTRANCE_REFUSAL`, which the app turns
    into its own sentence in both languages; one that is R4's rule alone carries `UNVETTED_REFUSAL`.
    """
    from .rpc import RpcError  # noqa: PLC0415 - policy.py is imported by rpc.py

    others = [p for p in problems if not is_entrance_problem(p) and not is_unvetted_problem(p)]
    if others:
        raise RpcError(code, prefix + "; ".join(others))
    # The not-yet-vetted rule (R4, AgDR-0076) is tagged the same way, when it is all there is.
    if all(is_entrance_problem(p) for p in problems):
        raise RpcError(code, prefix + "; ".join(problems), dict(ENTRANCE_REFUSAL))
    if all(is_unvetted_problem(p) for p in problems):
        raise RpcError(code, prefix + "; ".join(problems), dict(UNVETTED_REFUSAL))
    raise RpcError(code, prefix + "; ".join(problems))
# The refusal's `data`, so a surface that can show words (the app, the Managed tier) has both languages.
ENTRANCE_REFUSAL: Mapping[str, str] = MappingProxyType({"refusal": "entrance", "message_en": ENTRANCE_RULE, "message_ar": ENTRANCE_RULE_AR})


def _entrance_id(value: Any) -> str:
    """Lower-cased, as Home Assistant reads an id. Mirror of entranceIdOf."""
    return value.strip().lower() if isinstance(value, str) else ""


def entrance_service_closes(action: str, entity_id: str, positions: list[Any] | None) -> bool:
    """Whether a call to `action` on an entrance can only close it. Mirror of entranceServiceCloses."""
    ed = _domain(entity_id)
    if not ed or _domain(action) != ed:
        return False
    service = _fold(action)[len(ed) + 1 :]
    close, position = _ENTRANCE_CLOSING.get(ed, _ENTRANCE_CLOSING_DEFAULT)
    if service in close:
        return True
    return position is not None and service == position and positions is not None and len(positions) > 0 and all(_is_literal_zero(p) for p in positions)


def entrance_state_closes(entity_id: str, value: Any) -> bool:
    """A scene's stored state for an entrance that can only leave it shut. Mirror of entranceStateCloses."""
    shut = "closed" if (_domain(entity_id) or "") in _ENTRANCE_CLOSING else "off"
    if isinstance(value, str) and value == shut:
        return True
    if not isinstance(value, dict):
        return False
    return value.get("state") == shut and all(k == "current_position" and _is_literal_zero(v) for k, v in value.items() if k != "state")


def _scene_entrance_ids(node: dict[str, Any], entrances: frozenset[str]) -> list[str]:
    """Every entrance a scene service sets to something other than shut, or snapshots. Mirror of sceneEntranceIds."""
    out: list[str] = []
    for src in (node, node.get("data"), node.get("data_template")):
        if not isinstance(src, dict):
            continue
        entities = src.get("entities")
        if isinstance(entities, dict):
            out.extend(str(k) for k, v in entities.items() if _entrance_id(k) in entrances and not entrance_state_closes(str(k), v))
        else:
            out.extend(x for x in (entities if isinstance(entities, list) else [entities]) if _entrance_id(x) in entrances)
        out.extend(x for x in _entity_ids({"entity_id": src.get("snapshot_entities")}) if _entrance_id(x) in entrances)
    return out


def _entrance_domains(entrances: frozenset[str]) -> set[str]:
    return {d for e in entrances if (d := _domain(e))}


def _entrance_action_problems(node: dict[str, Any], action: str, ids: list[str], p: str, entrances: frozenset[str]) -> list[str]:
    """The entrance half of the action branch. Mirror of entranceActionProblems."""
    if not entrances:
        return []
    problems: list[str] = []
    positions = _positions_of(node)
    # The per-script service `script.<id>` starts that script: checked as an id of its own. Mirror of policy.ts.
    starts_itself = _domain(action) == "script" and _fold(action)[len("script.") :] not in _SCRIPT_NOT_STARTING
    for eid in [*ids, _fold(action)] if starts_itself else ids:
        if _entrance_id(eid) in entrances and not entrance_service_closes(action, eid, positions):
            problems.append(f"{p}: action {action} could open {eid}; {ENTRANCE_RULE}")
    dom = _domain(action)
    if dom == "scene":
        problems.extend(f"{p}: action {action} could leave {eid} open; {ENTRANCE_RULE}" for eid in _scene_entrance_ids(node, entrances))
    # Or a fan-out, which reaches an entrance of any domain; mirror of entranceActionProblems.
    fan_out = fans_out(action) and _fold(action) != "homeassistant.turn_off"
    if dom and (fan_out or (dom in _entrance_domains(entrances) and not entrance_service_closes(action, f"{dom}.any", positions))):
        keys, sel_ids, unknowable = _target_selection(node, action)
        for key in INDIRECT_TARGET_KEYS:
            if key in keys:
                problems.append(f"{p}: action {action} may not select by {key} in a home with an entrance; name the entities")
        if unknowable or any(_TEMPLATE_RE.search(i) or _fold(i) in ("all", "none") for i in sel_ids):
            problems.append(f"{p}: action {action} has a target that cannot be known before it runs, which a home with an entrance does not allow; name the entities")
    return problems


def _entrance_device_problems(node: dict[str, Any], p: str, entrances: frozenset[str]) -> list[str]:
    """The entrance half for a device action. Mirror of entranceDeviceProblems."""
    if not entrances:
        return []
    domain = node.get("domain")
    if not isinstance(domain, str) or not ("type" in node or "device_id" in node):
        return []
    d = _fold(domain)
    if d not in _entrance_domains(entrances):
        return []
    kind = node.get("type")
    folded = _fold(kind) if isinstance(kind, str) else ""
    if folded == _ENTRANCE_CLOSING_DEVICE_TYPES.get(d, "turn_off") or folded in _ENTRANCE_READ_DEVICE_TYPES:
        return []
    ids = _entity_ids(node)
    if not ids or any(_TEMPLATE_RE.search(i) or _entrance_id(i) in entrances for i in ids):
        return [f"{p}: device action {domain}.{kind if kind is not None else 'undefined'} could open an entrance; {ENTRANCE_RULE}"]
    return []


# Raw remote codes (#568). Mirror of REMOTE_SEND_COMMAND in policy.ts, and a separate rule on purpose
# so it can be dropped on its own: delete the `command` loop in `_remote_command_problems` (the caps
# stay), and the same loop in policy.ts. The platform allowlist is box-only: `handlers/remotes.py`.
#
# The owner ruled on 2026-11-08 that a model may send and learn remote commands, because the household
# chose what to teach the remote. A **raw** code is different: Broadlink plays any `command` starting
# `b64:` as the code itself (`broadlink/remote.py`), reaching a device nobody taught it. Matched
# case-insensitively, in a string or any list element. A command that cannot be read before it runs (a
# template, a templated key, a whole templated `data`, or a value that is not a string, number or
# boolean) is refused too, since a template could render to `b64:`. `send_command` only:
# `learn_command`'s `command` is the name a learned code is stored under.
_REMOTE_SEND_COMMAND = "remote.send_command"
_RAW_REMOTE_CODE_RE = re.compile(r"b64:", re.IGNORECASE)
_REMOTE_RAW_CODE_RULE = "Hearth sends only commands the household taught the remote, never a raw b64: code"


# How long one sent command may keep a transmitter busy (#568, Security's F4). Mirror of REMOTE_SEND_CAPS:
# `num_repeats` has no upper bound in HA and the two times are bare floats, so one call could hold an
# IR or RF transmitter (or a pressed key, through `hold_secs`) for hours. The `command` list is counted too
# (one string is one), or 500 names would undo the rest. A literal number in range only.
# Not part of the raw-code rule; dropping that rule keeps these.
_REMOTE_SEND_CAPS: tuple[tuple[str, int], ...] = (("num_repeats", 10), ("delay_secs", 60), ("hold_secs", 10))
_REMOTE_SEND_COMMANDS_MAX = 10
_REMOTE_SEND_CAPS_RULE = "Hearth sends at most 10 remote commands in one call, each at most 10 times, at most 60 s apart, held at most 10 s"


def _remote_command_problems(carriers: list[Any]) -> list[str]:
    """What is wrong with a `remote.send_command`'s `command` and caps. Mirror of remoteCommandProblems."""
    unknowable = f"sends a remote command Hearth cannot read until it runs; {_REMOTE_RAW_CODE_RULE}"
    found: list[str] = []
    for carrier in carriers:
        if carrier is None:
            continue
        if not isinstance(carrier, dict) or _has_templated_key(carrier):
            found.append(unknowable)
            continue
        command = carrier["command"] if "command" in carrier else []
        for item in command if isinstance(command, list) else [command]:
            if isinstance(item, str) and _RAW_REMOTE_CODE_RE.match(item):
                found.append(f"sends a raw remote code; {_REMOTE_RAW_CODE_RULE}")
            if (isinstance(item, str) and _TEMPLATE_RE.search(item)) or not isinstance(item, (str, int, float)):
                found.append(unknowable)
        if isinstance(command, list) and len(command) > _REMOTE_SEND_COMMANDS_MAX:
            found.append(f"sends more than {_REMOTE_SEND_COMMANDS_MAX} commands in one call; {_REMOTE_SEND_CAPS_RULE}")
        for key, cap in _REMOTE_SEND_CAPS:
            if key not in carrier:
                continue
            value = carrier[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= cap:
                found.append(f"sets {key} to something other than a number from 0 to {cap}; {_REMOTE_SEND_CAPS_RULE}")
    return list(dict.fromkeys(found))


# A remote activity Hearth cannot read until it runs (#765). Mirror of REMOTE_ACTIVITY_SERVICES in
# policy.ts. On HA 2026.9.3 `androidtv_remote` launches whatever `activity` names (an app it has a name
# for, else the value itself as an app id or a URL), `toggle` reaches it through `async_toggle`, and
# `homeassistant.turn_on`/`turn_off`/`toggle` forward their whole `data` to the remote's own service.
# Here only what cannot be compared with the remote's own `activity_list` is refused (a template, a
# non-string, a templated key, a whole templated `data`); the list itself is read on the box, in
# `handlers/remotes.py`, which can see the target's state.
_REMOTE_ACTIVITY_SERVICES: frozenset[str] = frozenset(
    {"remote.turn_on", "remote.turn_off", "remote.toggle", "homeassistant.turn_on", "homeassistant.turn_off", "homeassistant.toggle"}
)
_REMOTE_ACTIVITY_RULE = "Hearth starts only a remote activity the remote itself lists, named literally"

# Script variables on a direct call (#765). Mirror of VARIABLES_RULE in policy.ts. `homeassistant.turn_on`
# on a group holding a script forwards its whole `data` to `script.turn_on`, whose `variables` fill in the
# templates of a script the household wrote, so the caller would choose its targets. `devices.call` never
# names a script itself (ROUTINE_DOMAINS), so no call it makes has a use for `variables`. The box also
# refuses any data on a group that reaches a script (`control.member_violations`).
_VARIABLES_RULE = "a call may not pass variables; Hearth runs a household script only as it was written"


def _remote_activity_problems(carriers: list[Any]) -> list[str]:
    """What is wrong with the `activity` in each carrier (`data`, `data_template`). Mirror of remoteActivityProblems."""
    found: list[str] = []
    for carrier in carriers:
        if carrier is None:
            continue
        if not isinstance(carrier, dict) or _has_templated_key(carrier):
            found.append(f"carries data Hearth cannot read until it runs, which could start a remote activity; {_REMOTE_ACTIVITY_RULE}")
            continue
        if "activity" in carrier and (not isinstance(carrier["activity"], str) or _TEMPLATE_RE.search(carrier["activity"])):
            found.append(f"starts a remote activity Hearth cannot read until it runs; {_REMOTE_ACTIVITY_RULE}")
    return list(dict.fromkeys(found))


# A notification icon that names a file on the Home Assistant host (#771). Mirror of NOTIFY_DOMAIN in
# policy.ts. LG webOS's notify platform passes `data.icon` to aiowebostv's `send_message(icon_path=...)`,
# whose `read_icon` opens any path with no `is_allowed_path` check and sends it to the TV (HA 2026.9.3,
# `webostv/notify.py`), so `icon: /config/secrets.yaml` carried every integration's tokens off the box.
# Matched on shape, since notify service names are per household: in any `notify.*` action, a `data`
# mapping at any depth whose `icon` is not a literal `mdi:` icon name or a plain `http(s)` URL
# (`_notify_icon_allowed`, scheme in any case) is refused, as is a templated key, a whole templated `data`, or a templated nested
# `data`. Other attachment keys wait on the owner's ruling (#771). Write only on the hub, like the remote rules.
_NOTIFY_DOMAIN = "notify"
# The whole value is anchored (`fullmatch`), not only its prefix: webOS opens `mdi:/../../config/secrets.yaml`
# as a relative path. A URL is printable ASCII with no space, no backslash and no `..` segment. `re.ASCII` keeps
# IGNORECASE from folding U+212A or U+017F into `k`/`s`, which the hub's regex does not.
_NOTIFY_MDI_ICON_RE = re.compile(r"mdi:[a-z0-9_-]+", re.IGNORECASE | re.ASCII)
_NOTIFY_URL_ICON_RE = re.compile(r"https?://[\x21-\x7e]+", re.IGNORECASE | re.ASCII)
_NOTIFY_URL_ICON_BAD_RE = re.compile(r"\\|(^|/)\.\.(/|\Z|[?#])")


def _notify_icon_allowed(icon: str) -> bool:
    """Mirror of notifyIconAllowed."""
    return bool(_NOTIFY_MDI_ICON_RE.fullmatch(icon)) or (bool(_NOTIFY_URL_ICON_RE.fullmatch(icon)) and not _NOTIFY_URL_ICON_BAD_RE.search(icon))
_NOTIFY_ICON_RULE = "a notification icon must be a literal mdi: icon or an http(s) URL, never a file on the Home Assistant host"


def _notify_icon_problems(carriers: list[Any]) -> list[str]:
    """What is wrong with the `icon` in each carrier (`data`, `data_template`) of a `notify.*` action. Mirror of notifyIconProblems."""
    unknowable = f"carries notification data Hearth cannot read until it runs, which could name an icon file; {_NOTIFY_ICON_RULE}"
    found: list[str] = []

    def visit(mapping: dict[Any, Any]) -> None:
        if _has_templated_key(mapping):
            found.append(unknowable)
        if "icon" in mapping:
            icon = mapping["icon"]
            if not isinstance(icon, str) or _TEMPLATE_RE.search(icon) or not _notify_icon_allowed(icon):
                found.append(f"sets a notification icon that could be a file; {_NOTIFY_ICON_RULE}")
        if "data" in mapping:
            inner = mapping["data"]
            if isinstance(inner, dict):
                visit(inner)
            # A string is a template or a literal; a list or a scalar renders to itself. Anything else is
            # a `Template` from the second walk over the config as Home Assistant validated it.
            elif (isinstance(inner, str) and _TEMPLATE_RE.search(inner)) or not isinstance(inner, (str, int, float, list, type(None))):
                found.append(unknowable)

    for carrier in carriers:
        if carrier is None:
            continue
        if not isinstance(carrier, dict):
            found.append(unknowable)
        else:
            visit(carrier)
    return list(dict.fromkeys(found))


# A notification is never a command to the phone (R4 security review, AgDR-0076). Mirror of
# NOTIFY_COMMAND_PREFIXES in policy.ts, whose comment says why: the companion app carries these out
# rather than showing them. Every `notify.*` action, the `mobile_app` device action, `devices.call` and
# `notify.send`; a templated message is refused when its literal lead could still become a command.
# Android's and iOS's command words, one list (#812), as in policy.ts.
NOTIFY_COMMAND_PREFIXES: tuple[str, ...] = (
    "command_",
    "request_location_update",
    "remove_channel",
    "clear_notification",
    "update_complications",
    "update_widgets",
)
NOTIFY_EXACT_COMMANDS: tuple[str, ...] = ("tts",)
NOTIFY_COMMAND_RULE = (
    "a notification may not be a companion-app command (a message starting command_, request_location_update, "
    "remove_channel, clear_notification, update_complications or update_widgets, or one that is TTS), which a phone "
    "carries out rather than shows; start the message with literal words"
)


def _strip_message(text: str, *, lead: bool) -> str:
    """`str.strip`'s set plus U+FEFF, until nothing changes. Mirror of NOTIFY_STRIP_LEAD and its trailing twin."""
    while True:
        stripped = (text.lstrip().lstrip("\ufeff") if lead else text.rstrip().rstrip("\ufeff"))
        if stripped == text:
            return text
        text = stripped


def notify_message_problem(message: Any) -> str | None:
    """Why a notification `message` could be a companion-app command, or None. Mirror of notifyMessageProblem."""
    if message is None or isinstance(message, (bool, int, float)):
        return None
    # The second walk in `_validate` reads Home Assistant's validated config, where a message is a
    # `Template` whether or not it is templated: its source is what it renders from.
    source = getattr(message, "template", None)
    if not isinstance(message, str) and isinstance(source, str):
        message = source
    if not isinstance(message, str):
        return f"carries a message Hearth cannot read until it runs; {NOTIFY_COMMAND_RULE}"
    match = _TEMPLATE_RE.search(message)
    # U+FEFF too: JavaScript's `\s` strips it and `str.lstrip` does not.
    lead = _fold(_strip_message(message if match is None else message[: match.start()], lead=True))
    whole = _strip_message(lead, lead=False)
    if match is None:
        could = any(lead.startswith(c) for c in NOTIFY_COMMAND_PREFIXES) or whole in NOTIFY_EXACT_COMMANDS
    else:
        could = any(lead.startswith(c) or c.startswith(lead) for c in NOTIFY_COMMAND_PREFIXES) or any(c.startswith(whole) for c in NOTIFY_EXACT_COMMANDS)
    return f"sends a message that could be a command to a phone; {NOTIFY_COMMAND_RULE}" if could else None


def _notify_target_problems(target: Any) -> list[str]:
    """A notify action's `target`, which Home Assistant merges into the data. Mirror of notifyTargetProblems."""
    if target is None:
        return []
    if not isinstance(target, dict) or _has_templated_key(target):
        return [f"carries a target Hearth cannot read until it runs, which could carry a message; {NOTIFY_COMMAND_RULE}"]
    found: list[str] = []
    for key in ("message", "title"):
        if problem := notify_message_problem(target.get(key)):
            found.append(f"target {key} {problem}")
    return list(dict.fromkeys(found))


def _notify_command_problems(carriers: list[Any]) -> list[str]:
    """The same, for the `message` of each carrier of a `notify.*` action. Mirror of notifyCommandProblems."""
    found: list[str] = []
    for carrier in carriers:
        if isinstance(carrier, dict) and (problem := notify_message_problem(carrier.get("message"))):
            found.append(problem)
    return list(dict.fromkeys(found))


# A water heater's target is capped at 60 °C for safety (#560, AgDR-0075). Mirror of the
# WATER_HEATER_DOMAIN block in policy.ts, whose comment says why, and a separate block on purpose.
# Home Assistant's `set_temperature` takes any number and a scene sends its stored `temperature` the
# same way, so only the vendor stands between the value and the tank. These walkers know neither the
# home's unit nor the entity's `max_temp`, so they refuse a temperature that is not a literal number
# or is above 140 (over the ceiling in either unit); `handlers/water_heater.py` applies the exact
# ceiling, 60 °C or 140 °F or the entity's own `max_temp` if lower, wherever the box can read the home.
# Never clamped. A mode may be only off, eco, electric, gas or heat_pump, compared exactly; a scene
# Hearth writes may store only `on` or one of those, and a household's own scene keeps its mode when it
# runs (#713's line). `set_away_mode` is allowed (a setback; it can only lower the target); any other
# `water_heater` service is refused, an allowlist. A `scene.create` snapshot is not refused.
WATER_HEATER_DOMAIN = "water_heater"
WATER_HEATER_CEILING: Mapping[str, int] = MappingProxyType({"°C": 60, "°F": 140})
_WATER_HEATER_UNITLESS_CEILING = WATER_HEATER_CEILING["°F"]
_WATER_HEATER_SERVICES = ("turn_on", "turn_off", "set_away_mode", "set_temperature", "set_operation_mode")
WATER_HEATER_MODES: tuple[str, ...] = ("off", "eco", "electric", "gas", "heat_pump")
_WATER_HEATER_SCENE_STATES = ("on", *WATER_HEATER_MODES)
WATER_HEATER_CEILING_RULE = (
    "a water heater is capped at 60 °C (140 °F) for safety, or at its own max_temp if that is lower, and the value must be a literal number; Hearth refuses rather than lowers it, so set 60 °C or less, or set a higher value on the heater itself"
    " / سخان الماء محدود بـ 60 درجة مئوية (140 فهرنهايت) للسلامة، أو بالحد الأقصى للجهاز إن كان أقل؛ اختر 60 أو أقل، أو اضبط القيمة الأعلى من السخان نفسه"
)
WATER_HEATER_MODE_RULE = (
    "a water heater's mode may be set from Hearth only to off, eco, electric, gas or heat_pump, named literally; set any other mode on the heater itself"
    " / يمكن ضبط وضع سخان الماء من أذكى على off أو eco أو electric أو gas أو heat_pump فقط؛ اضبط أي وضع آخر من السخان نفسه"
)
_WATER_HEATER_SERVICE_RULE = "Hearth calls only turn_on, turn_off, set_away_mode, set_temperature and set_operation_mode on a water heater"


def is_literal_number(value: Any) -> bool:
    """A finite int or float. Not a bool (which Python calls a number), not a string: either needs coercing."""
    import math  # noqa: PLC0415

    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _water_heater_temperature_problems(value: Any) -> list[str]:
    """Mirror of waterHeaterTemperatureProblems."""
    if not is_literal_number(value):
        return [f"sets a temperature that is not a literal number; {WATER_HEATER_CEILING_RULE}"]
    if value > _WATER_HEATER_UNITLESS_CEILING:
        return [f"sets a temperature above the ceiling; {WATER_HEATER_CEILING_RULE}"]
    return []


def _water_heater_service_problems(service: str, carriers: list[Any]) -> list[str]:
    """What is wrong with a call to `water_heater.<service>` sending `carriers`. Mirror of waterHeaterServiceProblems."""
    s = _fold(service)
    if s not in _WATER_HEATER_SERVICES:
        return [f"is not a water heater service Hearth has checked; {_WATER_HEATER_SERVICE_RULE}"]
    if s not in ("set_temperature", "set_operation_mode"):
        return []
    problems: list[str] = []
    temperatures: list[Any] = []
    modes: list[Any] = []
    unreadable = False
    for carrier in carriers:
        if carrier is None:
            continue
        # A templated key is rendered, and the later of two equal keys wins, as for a valve's position.
        if not isinstance(carrier, dict) or _has_templated_key(carrier):
            unreadable = True
            continue
        if "temperature" in carrier:
            temperatures.append(carrier["temperature"])
        if "operation_mode" in carrier:
            modes.append(carrier["operation_mode"])
    rule = WATER_HEATER_CEILING_RULE if s == "set_temperature" else WATER_HEATER_MODE_RULE
    if unreadable:
        problems.append(f"carries data Hearth cannot read until it runs; {rule}")
    elif s == "set_temperature" and not temperatures:
        problems.append(f"names no temperature Hearth can check; {rule}")
    elif s == "set_operation_mode" and not modes:
        problems.append(f"names no mode Hearth can check; {rule}")
    for t in temperatures:
        problems.extend(_water_heater_temperature_problems(t))
    for m in modes:
        if not isinstance(m, str) or m not in WATER_HEATER_MODES:
            problems.append(f"sets a mode Hearth does not set; {WATER_HEATER_MODE_RULE}")
    return list(dict.fromkeys(problems))


def _water_heater_scene_temperature_problems(value: Any) -> list[str]:
    """Mirror of waterHeaterSceneTemperatureProblems."""
    return _water_heater_temperature_problems(value["temperature"]) if isinstance(value, dict) and "temperature" in value else []


def _water_heater_scene_mode_problems(value: Any) -> list[str]:
    """Mirror of waterHeaterSceneModeProblems: the state a **written** scene stores for a water heater."""
    state = value.get("state") if isinstance(value, dict) else value
    if isinstance(state, str) and state in _WATER_HEATER_SCENE_STATES:
        return []
    return [f"a scene may set a water heater only to on or one of its allowed modes; {WATER_HEATER_MODE_RULE}"]


def _scene_water_heater_problems(node: dict[str, Any]) -> list[str]:
    """Every water heater a scene service sets, and what is wrong with it. Mirror of sceneWaterHeaterProblems."""
    problems: list[str] = []
    for src in (node, node.get("data"), node.get("data_template")):
        if not isinstance(src, dict) or not isinstance(src.get("entities"), dict):
            continue
        for eid, v in src["entities"].items():
            if _domain(eid) == WATER_HEATER_DOMAIN:
                problems.extend(f"{eid}: {problem}" for problem in _water_heater_scene_mode_problems(v) + _water_heater_scene_temperature_problems(v))
    return problems


# A model calls an integration's own services only once each one is vetted (R4 of the council's ruling
# of 2026-11-09, #581, AgDR-0076). Mirror of ENTITY_SERVICE_DOMAINS and its neighbours in policy.ts,
# which say why; `test_policy.py` pins these lists to `schema/methods.json` and the entity domains to
# Home Assistant's own `Platform` enum. Which device a vetted service reaches is `handlers/vetted.py`.
ENTITY_SERVICE_DOMAINS: frozenset[str] = frozenset(
    {
        # Home Assistant's entity platforms (2026.9.3), less the raw senders in UNVETTABLE_DOMAINS.
        "ai_task", "air_quality", "alarm_control_panel", "assist_satellite", "binary_sensor", "button", "calendar", "camera",
        "climate", "conversation", "cover", "date", "datetime", "device_tracker", "event", "fan", "geo_location", "humidifier",
        "image", "image_processing", "lawn_mower", "light", "lock", "media_player", "notify", "number", "remote", "scene",
        "select", "sensor", "siren", "stt", "switch", "text", "time", "todo", "tts", "update", "vacuum", "valve", "wake_word",
        "water_heater", "weather",
        # Home Assistant's own core and helper domains.
        "homeassistant", "persistent_notification", "automation", "script", "group", "zone", "person", "counter", "timer",
        "schedule", "input_boolean", "input_button", "input_datetime", "input_number", "input_select", "input_text",
        # Home Assistant's own built-ins that act on their own data, not a device. Not `system_log`, not `alert`.
        "logbook", "shopping_list",
    }
)
VETTED_INTEGRATION_SERVICES: frozenset[str] = frozenset(
    {
        "alexa_devices.send_info_skill",
        "alexa_devices.send_sound",
        "frontend.set_theme",
        "openai_conversation.generate_image",
        "utility_meter.calibrate",
        "utility_meter.reset",
        "webostv.button",
        "webostv.select_sound_output",
    }
)
_MOBILE_APP_DOMAIN = "mobile_app"
VETTED_DEVICE_ACTIONS: frozenset[str] = frozenset({"mobile_app.notify"})
UNVETTABLE_SERVICES: frozenset[str] = frozenset({"mqtt.publish", "system_log.clear"})
UNVETTABLE_DOMAINS: frozenset[str] = frozenset({"infrared", "radio_frequency", "rfxtrx", "rflink", "pilight"})
UNVETTED_RULE = (
    "Azka lets an assistant use an integration's own services only once each one has been checked, and this one has not been yet. "
    "The home's owner can ask Azka to add it, or use it in Home Assistant"
)
UNVETTED_RULE_AR = (
    "لا يسمح أذكى للمساعد باستخدام خدمات التكامل الخاصة إلا بعد التحقق من كل منها، ولم يُتحقق من هذه بعد. "
    "يمكن لمالك المنزل أن يطلب من أذكى إضافتها، أو استخدامها من Home Assistant"
)
UNVETTED_REFUSAL: Mapping[str, str] = MappingProxyType({"refusal": "unvetted", "message_en": UNVETTED_RULE, "message_ar": UNVETTED_RULE_AR})
_VETTED_TARGET_RULE = "Azka checks which device an integration's service reaches, so name the device or entity literally"
# Keys an integration service addresses a device by, besides a target's `device_id`. Mirror of DEVICE_ADDRESS_KEYS.
DEVICE_ADDRESS_KEYS: tuple[str, ...] = ("device_id", "node_id", "ieee")
# Keys whose value is a list of triggers. Mirror of TRIGGER_KEYS.
_TRIGGER_KEYS: frozenset[str] = frozenset({"triggers", "trigger", "wait_for_trigger"})


def is_unvetted_problem(problem: str) -> bool:
    """Whether a refusal is this rule's own. Mirror of isUnvettedProblem."""
    return UNVETTED_RULE in problem


def is_vetted_service(action: str) -> bool:
    """Whether `action` is a standard entity-domain service, or a vetted integration one. Mirror of isVettedService."""
    d = _domain(action)
    if not d or d in UNVETTABLE_DOMAINS or _fold(action) in UNVETTABLE_SERVICES:
        return False
    return d in ENTITY_SERVICE_DOMAINS or _fold(action) in VETTED_INTEGRATION_SERVICES


def _denied_by_name(action: str) -> bool:
    """Already refused by name or domain, so "not yet vetted" would only repeat it. Mirror of deniedByName."""
    return _domain(action) in DENIED_ACTION_DOMAINS or _fold(action) in DENIED_ACTIONS


def _vetted_target_problems(node: dict[str, Any], action: str, what: str, p: str) -> list[str]:
    """What a vetted integration service or device action may not select by. Mirror of vettedTargetProblems."""
    problems: list[str] = []
    keys, ids, unknowable = _target_selection(node, action)
    for key in ("area_id", "floor_id", "label_id"):
        if key in keys:
            problems.append(f"{p}: {what} is an integration's own service and may not select by {key}; {_VETTED_TARGET_RULE}")
    values: list[Any] = []
    for src in (node, node.get("target"), node.get("data"), node.get("data_template")):
        if not isinstance(src, dict):
            continue
        for key in DEVICE_ADDRESS_KEYS:
            if key in src:
                value = src[key]
                values.extend(value if isinstance(value, list) else [value])

    def templated(v: Any) -> bool:
        return isinstance(v, str) and (bool(_TEMPLATE_RE.search(v)) or v.strip().lower() in ("all", "none"))

    if unknowable or any(templated(i) for i in ids) or any(templated(v) for v in values):
        problems.append(f"{p}: {what} is an integration's own service and what it reaches cannot be known before it runs; {_VETTED_TARGET_RULE}")
    return problems


# Domains handled by their own capability, so device control never implicitly grants the
# ability to run user-authored sequences.
ROUTINE_DOMAINS: frozenset[str] = frozenset({"scene", "script", "automation"})


TARGET_KEYS: frozenset[str] = frozenset({"entity_id", "device_id", "area_id", "floor_id", "label_id", "target"})
"""Keys that would widen a call past the entity_ids it names; `devices.call` strips them from `data`.
Mirror of TARGET_KEYS in policy.ts."""


def find_service_call_violations(
    domain: str, service: str, entity_ids: list[str], data: Any = None, entrances: frozenset[str] = _NO_ENTRANCES
) -> list[str]:
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
    # An integration's own service only once it is vetted (R4, AgDR-0076): see `ENTITY_SERVICE_DOMAINS`.
    elif domain not in DENIED_ACTION_DOMAINS and not is_vetted_service(folded):
        problems.append(f"{full}: not yet vetted; {UNVETTED_RULE}")
    if domain in ROUTINE_DOMAINS:
        problems.append(f"{full}: use activate_scene or run_script instead of calling the {raw_domain} domain directly")
    # Keyed on the service, not the ids: `valve.open_valve` reaches valves whatever it names (#527).
    positions = [data["position"]] if isinstance(data, dict) and "position" in data else []
    if domain == VALVE_DOMAIN and not _valve_service_closes(full, positions):
        problems.append(f"{full}: {_VALVE_CLOSING_RULE}")
    # A water heater's temperature and mode (#560): see `WATER_HEATER_DOMAIN`. Leaving `data` out
    # refuses `set_temperature`, as for a valve's position.
    if domain == WATER_HEATER_DOMAIN:
        problems.extend(f"{full}: {problem}" for problem in _water_heater_service_problems(service, [data]))
    # Raw remote codes (#568): see `_REMOTE_SEND_COMMAND`.
    if folded == _REMOTE_SEND_COMMAND:
        problems.extend(f"{full}: {problem}" for problem in _remote_command_problems([data]))
    # A remote activity (#765): see `_REMOTE_ACTIVITY_SERVICES`.
    if folded in _REMOTE_ACTIVITY_SERVICES:
        problems.extend(f"{full}: {problem}" for problem in _remote_activity_problems([data]))
    # A notification icon read from a file (#771): see `_NOTIFY_DOMAIN`. A backstop: the legacy notify
    # schema refuses most of what this reads, but the rule should not depend on that.
    if domain == _NOTIFY_DOMAIN:
        problems.extend(f"{full}: {problem}" for problem in _notify_icon_problems([data]))
        # A companion-app command sent as a notification (AgDR-0076): see `NOTIFY_COMMAND_PREFIXES`.
        # Not for a service denied by name, whose refusal already says it all.
        if not _denied_by_name(folded):
            problems.extend(f"{full}: {problem}" for problem in _notify_command_problems([data]))
    # Script variables (#765): see `_VARIABLES_RULE`.
    if isinstance(data, dict) and "variables" in data:
        problems.append(f"{full}: {_VARIABLES_RULE}")
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
        # An entrance only closes (R3): its own domain's closing service, whatever forwards to it.
        if _entrance_id(eid) in entrances and not entrance_service_closes(full, eid, positions):
            problems.append(f"{eid}: {ENTRANCE_RULE}")
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

HOST_SERVICE_DOMAINS: frozenset[str] = frozenset({"shell_command", "python_script", "hassio", "backup", "androidtv"})
"""Service domains that are host-level wherever they appear, including inside a template string.

`androidtv` is also in `DENIED_ACTION_DOMAINS` (#766); it is here too so that a form value or a
template that merely names `androidtv.adb_command` is refused by the reference scan, a backstop for a
service assembled where the action walk does not look (#764)."""

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


def find_policy_violations(
    config: Any,
    path: str = "config",
    *,
    registry_ids_resolved: bool = False,
    entrances: frozenset[str] = _NO_ENTRANCES,
    vet_services: bool = True,
) -> list[str]:
    """Human-readable violations for an automation or script config ([] = allowed). Mirror of `findPolicyViolations`.

    `registry_ids_resolved` is the box's one departure from the hub's walk, and only a caller that has
    already resolved entity registry ids (`handlers/resolve.py`) may pass it (#663). Such a caller has
    its own rule for an id that did not resolve: authoring refuses it in the resolver, without repeating
    a value that can be a BLE key in a flow form, and the run-time check on a labelled script lets it
    run, because HA's call raises on it and so reaches nothing (`scripts.script_run_violations`).
    Without it a registry id in a fan-out target is refused, as on the hub, which has no registry to
    resolve one against; that is the walk `POLICY_WALK_CASES` pins.

    `vet_services=False` drops R4's rule (an integration's own service only once it is vetted,
    AgDR-0076), and only a check of what the **household** wrote may pass it: a script one hop away
    from an authored config, and a labelled script when it runs. Everything Hearth writes, and every
    form value, keeps the default, which is the walk `POLICY_WALK_CASES` pins.
    """
    problems: list[str] = []

    # `in_trigger`: under a key that holds triggers, where a device node reads rather than acts (R4).
    def visit(node: Any, p: str, in_trigger: bool = False) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]", in_trigger)
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
            # An integration's own service only once it is vetted (R4, AgDR-0076), and then only at a
            # device the box can resolve.
            # Not under a trigger key: a node there reads, so only a denial by name applies to it.
            if vet_services and not in_trigger and dom and not _TEMPLATE_RE.search(action) and not _denied_by_name(action):
                if not is_vetted_service(action):
                    problems.append(f"{p}: action {action} is not yet vetted; {UNVETTED_RULE}")
                elif dom not in ENTITY_SERVICE_DOMAINS:
                    problems.extend(_vetted_target_problems(node, action, f"action {action}", p))
            ids = [i for k in TARGET_BEARING_KEYS for i in _entity_ids(node.get(k))] + _entity_ids(node) + _entity_ids({"entity_id": node.get("scene")})
            # A valve moves one way from here: closed (#527). The service decides, whatever it targets.
            if dom == VALVE_DOMAIN and not _valve_service_closes(action, _positions_of(node)):
                problems.append(f"{p}: action {action} is not a service that only closes a valve; {_VALVE_CLOSING_RULE}")
            # A water heater's temperature and mode (#560): see `WATER_HEATER_DOMAIN`.
            if dom == WATER_HEATER_DOMAIN:
                carriers = [node.get("data"), node.get("data_template")]
                problems.extend(f"{p}: action {action} {problem}" for problem in _water_heater_service_problems(_fold(action)[len(dom) + 1 :], carriers))
            # Raw remote codes (#568): see `_REMOTE_SEND_COMMAND`.
            if _fold(action) == _REMOTE_SEND_COMMAND:
                problems.extend(f"{p}: action {action} {problem}" for problem in _remote_command_problems([node.get("data"), node.get("data_template")]))
            # A remote activity (#765): see `_REMOTE_ACTIVITY_SERVICES`.
            if _fold(action) in _REMOTE_ACTIVITY_SERVICES:
                problems.extend(f"{p}: action {action} {problem}" for problem in _remote_activity_problems([node.get("data"), node.get("data_template")]))
            # A notification icon read from a file (#771): see `_NOTIFY_DOMAIN`.
            if dom == _NOTIFY_DOMAIN:
                problems.extend(f"{p}: action {action} {problem}" for problem in _notify_icon_problems([node.get("data"), node.get("data_template")]))
                # A companion-app command sent as a notification (AgDR-0076): see `NOTIFY_COMMAND_PREFIXES`.
                # Like the vetting rule, only for what Hearth writes: a household script one hop away may take
                # its message from an input (#777), and a labelled script is the household's own.
                if vet_services and not _denied_by_name(action):
                    problems.extend(f"{p}: action {action} {problem}" for problem in _notify_command_problems([node.get("data"), node.get("data_template")]))
                    problems.extend(f"{p}: action {action} {problem}" for problem in _notify_target_problems(node.get("target")))
            for eid in ids:
                if _domain(eid) == VALVE_DOMAIN and dom != VALVE_DOMAIN:
                    problems.append(f"{p}: action {action} aimed at {eid} could open a valve; {_VALVE_CLOSING_RULE}")
            # An entrance only closes (R3), after a delay or inside a scene's states as much as here.
            problems.extend(_entrance_action_problems(node, action, ids, p, entrances))
            if dom == "scene":
                problems.extend(f"{p}: action {action} could leave {eid} open; a scene may only close a valve" for eid in _scene_valve_ids(node))
                problems.extend(_scene_template_violations(node, action, p))
                # A water heater a scene service sets (#560): its temperature and its mode.
                problems.extend(f"{p}: action {action} sets {problem}" for problem in _scene_water_heater_problems(node))
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
        # An integration's device action as R4 reads one (AgDR-0076). Mirror of integrationDeviceAction.
        integration_device_action = (
            vet_services
            and not in_trigger
            and "device_id" in node
            and "condition" not in node
            and isinstance(device_domain, str)
            and _fold(device_domain) not in ENTITY_SERVICE_DOMAINS
            and _fold(device_domain) not in DENIED_ENTITY_DOMAINS
        )
        # Without a `type` it names nothing that could be vetted.
        if integration_device_action and not isinstance(device_type, str):
            problems.append(f"{p}: device action {device_domain} has no type Azka can read, so it is not yet vetted; {UNVETTED_RULE}")
        # The companion app's notify device action carries its message at the top level (AgDR-0076).
        if integration_device_action and _fold(device_domain) == _MOBILE_APP_DOMAIN:
            if problem := notify_message_problem(node.get("message")):
                problems.append(f"{p}: device action {device_domain}.{device_type} {problem}")
            problems.extend(f"{p}: device action {device_domain}.{device_type} {t}" for t in _notify_target_problems(node.get("target")))
        if isinstance(device_domain, str) and isinstance(device_type, str):
            named = f"{_fold(device_domain)}.{_fold(device_type)}"
            if DEVICE_ACTION_SERVICES.get(named, named) in DENIED_ACTIONS or named in DENIED_DEVICE_ACTIONS:
                problems.append(f"{p}: device action {device_domain}.{device_type} is not allowed")
            # An integration's device action only once it is vetted (R4, AgDR-0076). Only an action: a
            # node under a trigger key is a trigger, and one with a `condition` key is a condition
            # wherever it sits (`determine_script_action` takes `condition` before `device_id`).
            elif integration_device_action:
                if named not in VETTED_DEVICE_ACTIONS or _fold(device_domain) in UNVETTABLE_DOMAINS:
                    problems.append(f"{p}: device action {device_domain}.{device_type} is not yet vetted; {UNVETTED_RULE}")
                else:
                    problems.extend(_vetted_target_problems(node, "", f"device action {device_domain}.{device_type}", p))
        # A valve device action may only close (#527). Not exempted for a trigger or condition key:
        # `determine_script_action` settles a node carrying two action keys by `ACTIONS_MAP` order.
        if isinstance(device_domain, str) and _fold(device_domain) == VALVE_DOMAIN and ("type" in node or "device_id" in node) and node.get("type") != "close":
            problems.append(f"{p}: device action on a valve could open it; only type close is allowed")
        problems.extend(_entrance_device_problems(node, p, entrances))
        for k, v in node.items():
            if k in _LIST_KEYS or isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}", in_trigger or k in _TRIGGER_KEYS)

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


def find_scene_policy_violations(config: Any, entrances: frozenset[str] = _NO_ENTRANCES) -> list[str]:
    entities = config.get("entities") if isinstance(config, dict) else None
    if not isinstance(entities, dict):
        return []
    # A `group.*` key is not refused here: this is also the run-time verdict, and the run walks the
    # group's members instead (`find_scene_run_violations`, #714). Writing one is refused by
    # `find_scene_write_violations`.
    return [f"entities.{eid}: denied domain {_domain(eid)}" for eid in entities if _domain(eid) in DENIED_ENTITY_DOMAINS] + [
        f"entities.{eid}: a scene may only close a valve" for eid, v in entities.items() if _domain(eid) == VALVE_DOMAIN and not _valve_state_closes(v)
    ] + [
        # An entrance only closes (R3), so a scene may set one only shut. Run and write alike. Mirror of policy.ts.
        f"entities.{eid}: {ENTRANCE_RULE}" for eid, v in entities.items() if _entrance_id(eid) in entrances and not entrance_state_closes(str(eid), v)
    ] + [
        # A water heater's temperature (#560), when the scene is written and whenever it runs.
        f"entities.{eid}: {problem}"
        for eid, v in entities.items()
        if _domain(eid) == WATER_HEATER_DOMAIN
        for problem in _water_heater_scene_temperature_problems(v)
    ]


def find_scene_write_violations(config: Any) -> list[str]:
    """What a scene Hearth **writes** may not set, on top of `find_scene_policy_violations` (#713): a
    routine, and (since #714) a group.

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
    problems: list[str] = []
    for eid in entities:
        if _domain(eid) in ROUTINE_DOMAINS:
            problems.append(f"entities.{eid}: a scene may not set an automation, script or scene; switching a routine is not something an authored config may do")
        # A group's `reproduce_state` sets every member, so `{"group.doors": "unlocked"}` unlocks each
        # lock in it. Write only since #714: a household scene's groups are walked when it runs.
        if _domain(eid) == _GROUP_DOMAIN:
            problems.append(f"entities.{eid}: a scene may not set a group; what its members are cannot be checked here")
    # A water heater's mode (#560), at write only: a household's own scene keeps its mode when it runs.
    problems.extend(f"entities.{eid}: {problem}" for eid, v in entities.items() if _domain(eid) == WATER_HEATER_DOMAIN for problem in _water_heater_scene_mode_problems(v))
    return problems
