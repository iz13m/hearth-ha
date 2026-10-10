# Hearth AI — Home Assistant integration

Connects your Home Assistant to [Hearth](https://app.example.com) so an LLM can author automations, scenes, and scripts for you.

**What it can do:** list areas/entities/states (read-only), and create/edit/delete automations, scenes, and scripts — the same files the UI editor writes. If you switch them on, it can also operate your devices, activate scenes and run scripts, and walk you through adding new integrations.

**You choose what it may touch**, per installation, under Settings → Devices & services → Hearth AI → Configure. Controlling devices and running scenes/scripts are **off by default**. Only entities you exposed to Assist are ever visible — and only ones you exposed can be operated, including scenes and scripts. Home Assistant exposes lights, switches, blinds, heating and scenes by default but **not scripts, helpers or most sensors**, so a script you want Hearth to run has to be exposed under Settings → Voice assistants → Expose. Switch on *Choose which devices Hearth can see* and you can do that from the Hearth app instead — including taking a device back, which is the only way to remove one from everything Hearth sees without coming here. Locks, alarms, cameras and location entities are never offered. A scene that sets a lock, alarm, or camera is refused however it was written.

**What it can never do:** unlock doors, disarm alarms, view cameras, read location entities, or reach host-level services such as `shell_command`, `hassio`, or `homeassistant.restart` — not directly, and not by writing them into an automation. It also never sends a password, API key, or token when setting up an integration; those steps are handed back for you to finish in Home Assistant. Automations can be authored but never fired. Every rule is enforced inside this integration, independently of the Hearth cloud.

## Requirements
Home Assistant **2025.8 or newer** (the options flow uses `OptionsFlowWithReload`).

## Install
1. HACS → Integrations → ⋮ → *Custom repositories* → add `https://github.com/iz13m/hearth-ha` (category *Integration*).
2. Install **Hearth AI**, restart Home Assistant.
3. Settings → Devices & services → *Add integration* → **Hearth AI**.
4. Paste the pairing token from your Hearth dashboard. Done.

The integration opens an **outbound** WebSocket to Hearth; no ports, no Nabu Casa, no admin token leaves your box.

## Managed AI
If you subscribe to the Managed tier, a **Hearth AI** conversation agent appears under Settings → Voice assistants. Select it as the agent for an assistant and chat in the HA app or by voice.

## Bumping the Home Assistant pin

The pin lives in three lines, and a bump moves all of them: `pyproject.toml` (`pytest-homeassistant-custom-component==`, line 12) and `scripts/test.sh` (`PHCC_VERSION`, line 16, and `HA_VERSION`, line 17). Delete `.venv` afterwards so `scripts/test.sh` rebuilds it, then:

1. Run `scripts/test.sh -q` as usual.
2. **Re-run the setup-axis scan**: `.venv/bin/python scripts/setup_axis_path_fields.py`. It lists the config flows that are new, gone or changed against `scripts/setup_axis_baseline.json`, and exits 1 if there is any difference. The scan stays out of `pytest` because its output is not fit to drive a denial (`tests/test_policy.py` explains).
3. **Read every flow it reports as new or changed** (`config_flow.py` and what it imports) before merging the bump. The question is whether the field takes a host path whose content can come back out, as `local_file` and `upb` did; the mechanisms already established are in the baseline's `read` map. A new integration with a raw host-path field would otherwise arrive with every test green.
4. Record what you found in the baseline's `read` map, then run `--write-baseline`. Do that last: running it first turns the baseline into a copy of the new version.

Each baseline row records every match with its count and how many times the flow calls `is_allowed_path`, so a flow that already matched and gains a second path field is reported, not only a new flow. A test fails while the baseline's version stamp differs from the installed Home Assistant, which only says the bump has not been through this step; it does not run the scan.

The scan reads only each `config_flow.py`, so a clean result means "no new match there", not "no new path fields".

## Licence
Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 Cade Wan SA.
