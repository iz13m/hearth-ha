"""Assist conversation agent for the Managed tier. Forwards utterances to the hub."""

from __future__ import annotations

import logging
from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import intent
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SIGNAL_STATUS, HearthConfigEntry, device_info
from .client import HearthClient
from .const import CHAT_TIMEOUT_S
from .rpc import RpcError

_LOGGER = logging.getLogger(__name__)

OFFLINE_TEXT = "Hearth is not connected right now. Please check the Hearth AI integration."
ERROR_TEXT = "Sorry, Hearth could not process that request."


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HearthConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not entry.runtime_data.options.managed:
        return  # BYO / off: no in-HA assistant
    async_add_entities([HearthConversationEntity(entry, entry.runtime_data.client)])


class HearthConversationEntity(conversation.ConversationEntity):
    """Conversation entity backed by the Hearth hub."""

    _attr_has_entity_name = False
    _attr_name = "Hearth AI"  # -> conversation.hearth_ai
    _attr_supports_streaming = False

    def __init__(self, entry: ConfigEntry, client: HearthClient) -> None:
        self._entry = entry
        self._client = client
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = device_info(entry)

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return "*"

    @property
    def available(self) -> bool:
        return self._client.connected

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, f"{SIGNAL_STATUS}_{self._entry.entry_id}", self._status_changed)
        )

    @callback
    def _status_changed(self, _connected: bool) -> None:
        """Hub connection state changed; re-evaluate availability (event loop only)."""
        self.async_write_ha_state()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        response = intent.IntentResponse(language=user_input.language)
        conversation_id = user_input.conversation_id or chat_log.conversation_id
        if not self._client.connected:
            text = OFFLINE_TEXT
            response.async_set_error(intent.IntentResponseErrorCode.FAILED_TO_HANDLE, text)
        else:
            is_admin = False
            if user_input.context.user_id:
                user = await self.hass.auth.async_get_user(user_input.context.user_id)
                is_admin = bool(user and user.is_admin and user.is_active)
            params = {
                "conversation_id": conversation_id,
                "text": user_input.text,
                "language": user_input.language,
                "ha_user_id": user_input.context.user_id,
                "ha_user_is_admin": is_admin,
            }
            if model := self._entry.runtime_data.options.model:
                params["model"] = model
            try:
                result = await self._client.async_call("chat.process", params, timeout=CHAT_TIMEOUT_S)
                text = str(result.get("text") or ERROR_TEXT)
                response.async_set_speech(text)
            except RpcError as err:
                _LOGGER.warning("chat.process failed: %s %s", err.code, err.message)
                text = OFFLINE_TEXT if err.code in ("offline", "timeout") else ERROR_TEXT
                response.async_set_error(intent.IntentResponseErrorCode.FAILED_TO_HANDLE, text)
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=text)
        )
        return conversation.ConversationResult(response=response, conversation_id=conversation_id)
