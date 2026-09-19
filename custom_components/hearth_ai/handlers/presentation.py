"""Hand the hub the arrangement an admin authored in the Hearth panel (AgDR-0046)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ..presentation import async_load
from ..rpc import Dispatcher


async def presentation_get(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """The whole arrangement, as stored.

    A plain read of Hearth's own configuration — it names entities and says where they go, and
    carries no state and no attributes. Nothing is filtered by Assist exposure here: an arrangement
    is not a way to see anything, and the hub resolves it against the entities it was separately
    allowed to list, so a tile naming something unshared simply finds nothing there.
    """
    return await async_load(hass)


def register(d: Dispatcher) -> None:
    d.register("presentation.get", presentation_get)
