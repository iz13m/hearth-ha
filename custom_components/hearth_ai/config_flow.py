"""Config flow: paste a pairing token, exchange it for an install secret."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_HUB_URL,
    CONF_INSTALL_SECRET,
    CONF_INSTALLATION_ID,
    CONF_PAIRING_TOKEN,
    CONF_WS_URL,
    DEFAULT_HUB_URL,
    DOMAIN,
    INTEGRATION_VERSION,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PAIRING_TOKEN): str,
        vol.Optional(CONF_HUB_URL, default=DEFAULT_HUB_URL): str,
    }
)


class HearthConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user step."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            hub_url = user_input.get(CONF_HUB_URL, DEFAULT_HUB_URL).rstrip("/")
            token = user_input[CONF_PAIRING_TOKEN].strip()
            if not _url_allowed(hub_url):
                errors[CONF_HUB_URL] = "invalid_url"
            else:
                try:
                    result = await self._pair(hub_url, token)
                except _PairError as err:
                    errors["base"] = err.code
                else:
                    await self.async_set_unique_id(result["installation_id"])
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Hearth AI",
                        data={
                            CONF_HUB_URL: hub_url,
                            CONF_INSTALLATION_ID: result["installation_id"],
                            CONF_INSTALL_SECRET: result["install_secret"],
                            CONF_WS_URL: result["ws_url"],
                        },
                    )
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def _pair(self, hub_url: str, token: str) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        payload = {
            "pairing_token": token,
            "ha_version": HA_VERSION,
            "integration_version": INTEGRATION_VERSION,
            "name": self.hass.config.location_name or "Home",
        }
        try:
            async with session.post(f"{hub_url}/api/integration/pair", json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 401:
                    raise _PairError("invalid_token")
                if resp.status == 429:
                    raise _PairError("rate_limited")
                if resp.status != 200:
                    _LOGGER.warning("pairing failed: HTTP %s", resp.status)
                    raise _PairError("cannot_connect")
                data = await resp.json()
        except _PairError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("pairing request failed: %s", err)
            raise _PairError("cannot_connect") from err
        for key in ("installation_id", "install_secret", "ws_url"):
            if not isinstance(data.get(key), str):
                raise _PairError("cannot_connect")
        ws = data["ws_url"]
        if not (ws.startswith("wss://") or (ws.startswith("ws://") and ws[5:].split("/", 1)[0].split(":", 1)[0] in _DEV_HOSTS)):
            raise _PairError("cannot_connect")  # the install secret never travels in cleartext
        return data


_DEV_HOSTS = ("localhost", "127.0.0.1", "host.docker.internal")


def _url_allowed(url: str) -> bool:
    """https only; plain http tolerated for local development hosts."""
    if url.startswith("https://"):
        return True
    if url.startswith("http://"):
        host = url[7:].split("/", 1)[0].split(":", 1)[0]
        return host in _DEV_HOSTS
    return False


class _PairError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- options flow
from homeassistant.config_entries import ConfigEntryState, OptionsFlowWithReload  # noqa: E402
from homeassistant.core import callback  # noqa: E402
from homeassistant.helpers.selector import (  # noqa: E402
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (  # noqa: E402
    ACCOUNT_STATUS_TIMEOUT_S,
    ASSISTANT_MODES,
    DEFAULT_MODEL,
    DEFAULT_MODELS,
    OPT_ASSISTANT_MODE,
    OPT_CONNECTION_ENABLED,
    OPT_MODEL,
    TOGGLEABLE_CAPABILITIES,
    option_key,
)
from .options import HearthOptions  # noqa: E402
from .rpc import RpcError  # noqa: E402

MENU = ["connection", "capabilities", "assistant"]


class HearthOptionsFlow(OptionsFlowWithReload):
    """Connection on/off, capability toggles, assistant mode + model. Saving reloads the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=MENU)

    # ---- connection
    async def async_step_connection(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        opts = HearthOptions.from_entry(self.config_entry)
        client = self._client()
        status = "disabled" if not opts.connection_enabled else ("connected" if client and client.connected else "disconnected")
        return self.async_show_form(
            step_id="connection",
            data_schema=vol.Schema({vol.Required(OPT_CONNECTION_ENABLED, default=opts.connection_enabled): BooleanSelector()}),
            description_placeholders={
                "installation_id": str(self.config_entry.data.get(CONF_INSTALLATION_ID, "?")),
                "hub_url": str(self.config_entry.data.get(CONF_HUB_URL, "")),
                "status": status,
            },
        )

    # ---- capabilities
    async def async_step_capabilities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        opts = HearthOptions.from_entry(self.config_entry)
        schema = vol.Schema(
            {vol.Required(option_key(cap), default=cap in opts.enabled_caps): BooleanSelector() for cap in TOGGLEABLE_CAPABILITIES}
        )
        return self.async_show_form(step_id="capabilities", data_schema=schema)

    # ---- assistant
    async def async_step_assistant(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        opts = HearthOptions.from_entry(self.config_entry)
        account = await self._fetch_account()
        models: list[str] = list(account.get("models") or DEFAULT_MODELS)
        errors: dict[str, str] = {}
        if user_input is not None:
            model = str(user_input.get(OPT_MODEL) or "")
            if model and model not in models:
                errors[OPT_MODEL] = "invalid_model"
            else:
                return self._save({OPT_ASSISTANT_MODE: user_input[OPT_ASSISTANT_MODE], OPT_MODEL: model})
        hub_url = str(self.config_entry.data.get(CONF_HUB_URL, "")).rstrip("/")
        model_options = [SelectOptionDict(value="", label=f"Hearth default ({account.get('default_model', DEFAULT_MODEL)})")] + [
            SelectOptionDict(value=m, label=m) for m in models
        ]
        schema = vol.Schema(
            {
                vol.Required(OPT_ASSISTANT_MODE, default=opts.assistant_mode): SelectSelector(
                    SelectSelectorConfig(options=list(ASSISTANT_MODES), mode=SelectSelectorMode.DROPDOWN, translation_key="assistant_mode")
                ),
                vol.Optional(OPT_MODEL, default=opts.model if opts.model in models else ""): SelectSelector(
                    SelectSelectorConfig(options=model_options, mode=SelectSelectorMode.DROPDOWN)
                ),
            }
        )
        period = account.get("period_end")
        return self.async_show_form(
            step_id="assistant",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "tier": str(account.get("tier") or "none"),
                "active": "yes" if account.get("active") else "no",
                "period_end": str(period)[:10] if period else "—",
                "dashboard_url": str(account.get("dashboard_url") or f"{hub_url}/app"),
                "mcp_url": str(account.get("mcp_url") or f"{hub_url}/mcp"),
                "models_source": "live" if account.get("_live") else "offline defaults",
            },
        )

    # ---- helpers
    def _client(self):
        entry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return None
        data = getattr(entry, "runtime_data", None)
        return getattr(data, "client", None)

    async def _fetch_account(self) -> dict[str, Any]:
        """Subscription + model list from the hub; static defaults when offline."""
        client = self._client()
        if client is not None and client.connected:
            try:
                result = await client.async_call("account.status", {}, timeout=ACCOUNT_STATUS_TIMEOUT_S)
                return {**result, "_live": True}
            except RpcError as err:
                _LOGGER.debug("account.status unavailable: %s", err.message)
        return {"tier": "unknown", "active": False, "period_end": None, "models": list(DEFAULT_MODELS), "default_model": DEFAULT_MODEL, "_live": False}

    def _save(self, changes: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})


@staticmethod
@callback
def _get_options_flow(config_entry: ConfigEntry) -> HearthOptionsFlow:  # pragma: no cover - trivial
    return HearthOptionsFlow()


HearthConfigFlow.async_get_options_flow = _get_options_flow  # type: ignore[assignment]
