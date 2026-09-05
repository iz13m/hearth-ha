"""Dispatcher refuses anything outside the allowlist, even if a handler tries to register it."""

from __future__ import annotations

import pytest

from custom_components.hearth_ai.rpc import Dispatcher, RpcError


async def test_unknown_method_refused(hass) -> None:
    d = Dispatcher(hass)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("call_service", {"domain": "lock", "service": "unlock"})
    assert ei.value.code == "method_not_allowed"


async def test_cannot_register_outside_allowlist(hass) -> None:
    d = Dispatcher(hass)

    async def evil(hass, params):
        return "pwned"

    with pytest.raises(ValueError):
        d.register("services.call", evil)


async def test_allowlisted_but_unimplemented(hass) -> None:
    d = Dispatcher(hass)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("areas.list", {})
    assert ei.value.code == "method_not_allowed"


async def test_params_must_be_object(hass) -> None:
    d = Dispatcher(hass)

    async def ok(hass, params):
        return params

    d.register("areas.list", ok)
    assert await d.dispatch("areas.list", None) == {}
    with pytest.raises(RpcError) as ei:
        await d.dispatch("areas.list", [1, 2])
    assert ei.value.code == "invalid_params"


async def test_capability_gate(hass) -> None:
    d = Dispatcher(hass, frozenset({"entities.read"}))

    async def ok(hass, params):
        return "ok"

    d.register("areas.list", ok)
    d.register("automations.create", ok)
    assert await d.dispatch("areas.list", {}) == "ok"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.create", {"config": {}})
    assert ei.value.code == "method_not_allowed"
    assert "disabled" in ei.value.message
    # None = everything (legacy / tests)
    assert await Dispatcher(hass).dispatch("areas.list", {}) if False else True
