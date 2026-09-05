# Hearth AI — Home Assistant integration

Connects your Home Assistant to [Hearth](https://app.example.com) so an LLM can author automations, scenes, and scripts for you.

**What it can do:** list areas/entities/states (read-only), and create/edit/delete automations, scenes, and scripts — the same files the UI editor writes.

**What it can never do:** call services, trigger automations, run scripts, unlock doors, view cameras. The method allowlist is enforced inside this integration, independently of the Hearth cloud.

## Requirements
Home Assistant **2025.8 or newer** (the options flow uses `OptionsFlowWithReload`).

## Install
1. HACS → Integrations → ⋮ → *Custom repositories* → add this repo (category *Integration*).
2. Install **Hearth AI**, restart Home Assistant.
3. Settings → Devices & services → *Add integration* → **Hearth AI**.
4. Paste the pairing token from your Hearth dashboard. Done.

The integration opens an **outbound** WebSocket to Hearth; no ports, no Nabu Casa, no admin token leaves your box.

## Managed AI
If you subscribe to the Managed tier, a **Hearth AI** conversation agent appears under Settings → Voice assistants. Select it as the agent for an assistant and chat in the HA app or by voice.
