"""What Devices & services calls this entry, and why it is migrated rather than left alone.

Home Assistant writes the config entry's title **once**, when the entry is created, and never
re-reads `manifest.json` for an entry that already exists. So renaming the manifest alone would
give a new install "Azka AI" and leave every already-paired home reading "Hearth AI" indefinitely —
and every instruction of the form "Settings -> Devices & services -> <name> -> Configure" would
then have no single correct value (AgDR-0063).

The migration is deliberately conditional. The title is also what an owner types when they rename
the entry themselves, so it is moved **only** when it still holds the old default: the harmful
path is not taken rather than merely covered.

**The old title is written here as a literal on purpose.** It is a fact about bytes already sitting
in customers' `.storage`, not a fact about this tree, so a test must not read it out of the module
it is proving compatibility with — `LEGACY_ENTRY_TITLE = "Azka AI"` would then make the condition
agree with itself and leave every paired home un-migrated under a green suite. The *destination*
does follow the source, because that one is supposed to move with the brand.
"""

from __future__ import annotations

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai.const import DOMAIN, ENTRY_TITLE

from .test_conversation import ENTRY_DATA


async def _setup(hass: HomeAssistant, title: str) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-1", data=ENTRY_DATA, title=title)
    entry.add_to_hass(hass)
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_a_home_still_on_the_old_default_is_migrated(hass: HomeAssistant) -> None:
    entry = await _setup(hass, "Hearth AI")  # the literal shipped installs wrote to `.storage`
    assert entry.title == ENTRY_TITLE == "Azka AI"


async def test_a_title_the_owner_typed_is_left_alone(hass: HomeAssistant) -> None:
    """The arm that matters: this is the case where migrating would do harm.

    An owner who renamed the entry — "Kitchen hub", a second home's name — chose that string. A
    rename that overwrote it would be this release reaching into their Home Assistant to undo an
    edit they made on purpose, and nothing would tell them it had happened.
    """
    entry = await _setup(hass, "Kitchen hub")
    assert entry.title == "Kitchen hub"


async def test_running_it_again_changes_nothing(hass: HomeAssistant) -> None:
    """Setup re-runs on every options save, so the migration has to be a no-op after the first."""
    entry = await _setup(hass, "Hearth AI")  # the literal shipped installs wrote to `.storage`
    assert entry.title == ENTRY_TITLE

    with patch("custom_components.hearth_ai.HearthClient.start"):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.title == ENTRY_TITLE
