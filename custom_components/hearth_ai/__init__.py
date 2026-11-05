"""Hearth AI — lets an LLM author automations, scenes, and scripts via the Hearth hub."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.start import async_at_started

from .claim import async_confirm, async_forget as async_forget_claim, async_pending_secret, async_save_pending, entry_claimed
from .client import HearthClient
from .const import CONF_CONFIRMED, CONF_INSTALL_SECRET, CONF_WS_URL, DOMAIN, ENTRY_TITLE, INTEGRATION_VERSION, LEGACY_ENTRY_TITLE
from .labels import async_ensure_labels
from .options import HearthOptions
from .panel import async_register_panel, async_unregister_panel
from .presentation import async_load as async_load_profile
from .rpc import build_dispatcher

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CONVERSATION, Platform.SWITCH]
SIGNAL_STATUS = f"{DOMAIN}_status"


@dataclass
class HearthData:
    client: HearthClient
    options: HearthOptions


type HearthConfigEntry = ConfigEntry[HearthData]


def device_info(entry: ConfigEntry) -> dr.DeviceInfo:
    """One device groups the connection switch, connectivity sensor, and conversation agent."""
    # The name is what the owner reads on the device page, so it is Azka; the entity ids under it
    # are pinned to `DOMAIN` in each platform and do not follow it (AgDR-0063).
    return dr.DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Azka AI", manufacturer="Azka", entry_type=dr.DeviceEntryType.SERVICE)


async def async_setup_entry(hass: HomeAssistant, entry: HearthConfigEntry) -> bool:
    """Set up from a config entry (re-run on every options change via OptionsFlowWithReload)."""
    # Devices & services reads the stored entry title, not `manifest.json`, so the brand rename
    # reaches an already-paired home only if we move it here (AgDR-0063). **Only the old default
    # is migrated**: the title is also what the owner types when they rename the entry themselves,
    # and overwriting that would be this change reaching into their home to undo their own edit.
    # A home that renamed it to exactly "Hearth AI" is indistinguishable from an untouched one and
    # gets the new name; that is the one case this cannot separate, and it is harmless.
    if entry.title == LEGACY_ENTRY_TITLE:
        hass.config_entries.async_update_entry(entry, title=ENTRY_TITLE)
    # Before the client: the first `entities.list` after connecting should already know the labels,
    # and the first `presentation.get` should not be the thing that reads the arrangement off disk.
    await async_ensure_labels(hass)
    await async_load_profile(hass)
    # The sidebar is local and has nothing to do with the connection, so a home whose hub is down
    # can still be arranged — and a home that has not finished pairing can be arranged first.
    await async_register_panel(hass, INTEGRATION_VERSION)
    # A secret offered in `claim.approve` that no `hello` has proven yet (the reply was lost, or Home
    # Assistant stopped before the entry's delayed save): the client tries it before the entry's own.
    pending_secret = await async_pending_secret(hass, entry)
    options = HearthOptions.from_entry(entry)
    capabilities = options.capabilities
    dispatcher = build_dispatcher(hass, frozenset(capabilities))

    def _status(connected: bool) -> None:
        async_dispatcher_send(hass, f"{SIGNAL_STATUS}_{entry.entry_id}", connected)

    client = HearthClient(
        hass,
        ws_url=entry.data[CONF_WS_URL],
        install_secret=entry.data[CONF_INSTALL_SECRET],
        dispatcher=dispatcher,
        on_status=_status,
        capabilities=capabilities,
        claimed=entry_claimed(entry),
        confirmed=entry.data.get(CONF_CONFIRMED, True) is not False,
        on_confirmed=lambda confirmed: _remember_confirmed(hass, entry, confirmed),
        pending_secret=pending_secret,
        on_secret_pending=lambda secret: async_save_pending(hass, entry, secret),
        on_claimed=lambda info: async_confirm(hass, entry, info),
        # The hub forgot this unclaimed home: Home Assistant's own "needs attention" flow re-registers.
        on_expired=lambda: entry.async_start_reauth(hass),
    )
    entry.runtime_data = HearthData(client=client, options=options)

    if not options.managed:
        # Don't leave a stale, permanently-unavailable conversation agent behind.
        ent_reg = er.async_get(hass)
        if entity_id := ent_reg.async_get_entity_id("conversation", DOMAIN, entry.entry_id):
            ent_reg.async_remove(entity_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if options.connection_enabled:
        client.start()
    # The other half of `ha.started` (#235): the client sends it after `hello` if HA is already
    # running, and this sends it once HA gets there. On an options reload HA is already running, so
    # this fires at once, before any socket exists, and the client's own gate makes it a no-op.
    entry.async_on_unload(async_at_started(hass, client.async_ha_started))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HearthConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.client.stop()
    # Setup re-runs on every options save (OptionsFlowWithReload), and so does unload — so the panel
    # stays registered here and only goes when the last entry is really leaving.
    if len(hass.config_entries.async_entries(DOMAIN)) <= 1:
        async_unregister_panel(hass)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _remember_confirmed(hass: HomeAssistant, entry: HearthConfigEntry, confirmed: bool) -> None:
    """Keep what the last hello said about the owner's confirmation across a restart."""
    data = {k: v for k, v in entry.data.items() if k != CONF_CONFIRMED}
    if not confirmed:
        data[CONF_CONFIRMED] = False
    hass.config_entries.async_update_entry(entry, data=data)


async def async_remove_entry(hass: HomeAssistant, entry: HearthConfigEntry) -> None:
    """The entry is deleted: so is any install secret kept from claiming it."""
    await async_forget_claim(hass, entry)
