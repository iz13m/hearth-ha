"""Claiming a home from inside Home Assistant (AgDR-0064).

A box that registered without a pairing token holds an install secret for an **unclaimed** home:
its socket may send only `UNCLAIMED_HA_TO_HUB_METHODS`, and the hub calls nothing on it. An HA admin
pastes the owner's claim link into the Azka panel, the box asks the hub whose it is, and the admin
approves or declines.

Approving rotates the install secret, and **the box makes the new one**: it is generated here, written
to this module's own private store with an awaited atomic write, and only then sent as `new_secret`
in `claim.approve`. The hub keeps only its hash and returns no secret. So nothing a crash, a lost
reply or a dropped socket can do leaves the box without the secret the hub now expects: until a
`hello` on the new secret proves it, the box keeps **both**, and every connect tries the new one first
and falls back to the old.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CLAIM_TOKEN_PATTERN,
    INSTALL_SECRET_PATTERN,
    CONF_CLAIMED,
    CONF_INSTALL_SECRET,
    CONF_INSTALLATION_ID,
    CONF_OWNER_EMAIL,
    CONF_UNCLAIMED_EXPIRES_AT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.claim"
# Entries confirmed on a new secret in this process. Their record must outlive this run: the config
# entry's own save is delayed, so only a later setup, which read the entry back from disk, may retire it.
DATA_CLAIMED_THIS_RUN = f"{DOMAIN}_claimed_this_run"
# The store is one file for every entry: its read-modify-write must not interleave, or two entries
# approving at once could each save a copy missing the other's record. One lock per Home Assistant
# (kept in `hass.data`), since an asyncio lock belongs to one event loop.
DATA_STORE_LOCK = f"{DOMAIN}_claim_store_lock"


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    return hass.data.setdefault(DATA_STORE_LOCK, asyncio.Lock())

# The same expression as `extractClaimToken` in packages/shared/src/methods.ts: the token alone, or
# inside the `hearth://claim/<token>` link, and never a longer run of token characters.
_EXTRACT = re.compile(rf"(?:^|[^A-Za-z0-9_-])({CLAIM_TOKEN_PATTERN})(?![A-Za-z0-9_-])")


def extract_claim_token(text: Any) -> str | None:
    """The claim token in whatever the admin pasted, or None. Mirrors `extractClaimToken`."""
    if not isinstance(text, str):
        return None
    m = _EXTRACT.search(text.strip())
    return m.group(1) if m else None


def new_install_secret(current: str | None = None) -> str:
    """The same shape and strength the hub's own `newSecret("his")` makes: 32 random bytes, base64url.

    Never the secret in use: the hub refuses a `new_secret` equal to the pre-claim one.
    """
    while True:
        secret = "his_" + secrets.token_urlsafe(32)
        if secret != current and re.fullmatch(INSTALL_SECRET_PATTERN, secret):
            return secret


def entry_claimed(entry: ConfigEntry) -> bool:
    """What the entry says. No `claimed` key means it was paired with a token, and is claimed."""
    return entry.data.get(CONF_CLAIMED) is not False


def _store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    # `private`: 0600, like the config entries file that holds the same kind of secret.
    return Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True)


async def async_save_pending(hass: HomeAssistant, entry: ConfigEntry, secret: str | None) -> None:
    """Write down the secret about to be offered in `claim.approve` (or forget it: None).

    Awaited before the approval is sent, and atomic, so the hub can never hold the hash of a secret
    this box has not kept.
    """
    async with _lock(hass):
        store = _store(hass)
        records = await store.async_load() or {}
        if secret is None:
            if records.pop(entry.entry_id, None) is None:
                return
        else:
            # Bound to the unclaimed id it was made for, so a later registration of this entry under a
            # new id can never be offered a stale secret.
            records[entry.entry_id] = {"installation_id": entry.data[CONF_INSTALLATION_ID], "new_secret": secret}
        await store.async_save(records)


async def async_pending_secret(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """At setup: a new secret offered and not yet confirmed, to try before the entry's own.

    That is the case after a restart inside the window between sending `claim.approve` and the
    entry's delayed save of the confirmed secret. A record that no longer applies is dropped.
    """
    async with _lock(hass):
        store = _store(hass)
        records = await store.async_load()
        if not records or entry.entry_id not in records:
            return None
        record = records[entry.entry_id]
        if not entry_claimed(entry) and record.get("installation_id") == entry.data.get(CONF_INSTALLATION_ID):
            return str(record["new_secret"])
        if entry.entry_id in hass.data.get(DATA_CLAIMED_THIS_RUN, set()):
            return None  # confirmed in this run; the entry's own save may still be pending
        records.pop(entry.entry_id)
        await store.async_save(records)
        return None


async def async_confirm(hass: HomeAssistant, entry: ConfigEntry, info: dict[str, Any]) -> None:
    """The hub accepted the new secret: it is the entry's now, and the home is claimed."""
    hass.data.setdefault(DATA_CLAIMED_THIS_RUN, set()).add(entry.entry_id)
    data = {k: v for k, v in entry.data.items() if k != CONF_UNCLAIMED_EXPIRES_AT}
    data[CONF_INSTALL_SECRET] = info["install_secret"]
    data[CONF_CLAIMED] = True
    if isinstance(info.get("owner_email"), str):
        data[CONF_OWNER_EMAIL] = info["owner_email"]
    installation_id = info.get("installation_id")
    if isinstance(installation_id, str) and installation_id != entry.data.get(CONF_INSTALLATION_ID):
        data[CONF_INSTALLATION_ID] = installation_id
        hass.config_entries.async_update_entry(entry, data=data, unique_id=installation_id)
        return
    hass.config_entries.async_update_entry(entry, data=data)


async def async_forget(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The entry is being removed: any secret kept for it goes too."""
    await async_save_pending(hass, entry, None)
