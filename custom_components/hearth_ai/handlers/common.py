"""Shared helpers for handlers: YAML read/write mirroring homeassistant.components.config.view."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.yaml import dump, load_yaml

from ..rpc import RpcError

# One lock per config file; HA's own config views use a per-view lock the same way.
_LOCKS: dict[str, asyncio.Lock] = {}


def lock_for(path: str) -> asyncio.Lock:
    return _LOCKS.setdefault(path, asyncio.Lock())


def _read(path: str) -> Any:
    if not os.path.isfile(path):
        return None
    return load_yaml(path)


def _write(path: str, data: dict | list) -> None:
    contents = dump(data)  # dump first so a serialisation error never truncates the file
    write_utf8_file_atomic(path, contents)


async def read_yaml(hass: HomeAssistant, path: str, empty: Any) -> Any:
    current = await hass.async_add_executor_job(_read, path)
    if not current:
        return empty
    return current


async def write_yaml(hass: HomeAssistant, path: str, data: dict | list) -> None:
    await hass.async_add_executor_job(_write, path, data)


def require_str(params: dict[str, Any], key: str) -> str:
    v = params.get(key)
    if not isinstance(v, str) or not v:
        raise RpcError("invalid_params", f"{key} must be a non-empty string")
    return v


def require_config(params: dict[str, Any]) -> dict[str, Any]:
    cfg = params.get("config")
    if not isinstance(cfg, dict):
        raise RpcError("invalid_params", "config must be an object")
    return dict(cfg)


def plain(value: Any) -> Any:
    """Convert HA's YAML node types (NodeStrClass, NodeDictClass, ...) to JSON-safe primitives."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return str(value)
