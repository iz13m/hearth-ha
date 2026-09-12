"""The physical device name an entity reports, used by the app to name a multi-gang switch plate."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.hearth_ai.handlers.registry import MAX_ATTR_STR, _device_name


class _Registry:
    def __init__(self, devices: dict[str, SimpleNamespace]) -> None:
        self._devices = devices

    def async_get(self, device_id: str) -> SimpleNamespace | None:
        return self._devices.get(device_id)


def _ent(device_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(device_id=device_id)


def test_prefers_the_name_the_owner_typed() -> None:
    # `name` is usually the manufacturer's model string; `name_by_user` is what the owner called it.
    reg = _Registry({"d1": SimpleNamespace(name="Shelly 3-gang Pro", name_by_user="Switch by the door")})
    assert _device_name(_ent("d1"), reg) == "Switch by the door"


def test_falls_back_to_the_manufacturer_name() -> None:
    reg = _Registry({"d1": SimpleNamespace(name="Shelly 3-gang Pro", name_by_user=None)})
    assert _device_name(_ent("d1"), reg) == "Shelly 3-gang Pro"


def test_is_none_without_a_device() -> None:
    reg = _Registry({})
    assert _device_name(None, reg) is None
    assert _device_name(_ent(None), reg) is None
    assert _device_name(_ent("gone"), reg) is None
    assert _device_name(_ent("d1"), None) is None


def test_is_none_for_an_empty_or_non_string_name() -> None:
    assert _device_name(_ent("d1"), _Registry({"d1": SimpleNamespace(name="", name_by_user="")})) is None
    assert _device_name(_ent("d1"), _Registry({"d1": SimpleNamespace(name=42, name_by_user=None)})) is None


def test_caps_the_length_like_any_other_string_off_the_lan() -> None:
    long = "x" * (MAX_ATTR_STR + 50)
    reg = _Registry({"d1": SimpleNamespace(name=long, name_by_user=None)})
    assert len(_device_name(_ent("d1"), reg) or "") == MAX_ATTR_STR
