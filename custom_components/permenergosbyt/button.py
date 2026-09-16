"""Button entity to trigger a manual send of the meter readings."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ACCOUNT, DOMAIN
from .scheduler import PermEnergosbytManager


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: PermEnergosbytManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PermEnergosbytSendButton(entry, manager)])


class PermEnergosbytSendButton(ButtonEntity):
    """Manually send the current T1/T2 readings right now (no retry)."""

    _attr_has_entity_name = True
    _attr_name = "Передать показания сейчас"
    _attr_icon = "mdi:send"

    def __init__(self, entry: ConfigEntry, manager: PermEnergosbytManager) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_send_now"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Пермэнергосбыт {entry.data[CONF_ACCOUNT]}",
            "manufacturer": "ПАО Пермэнергосбыт",
        }

    async def async_press(self) -> None:
        await self._manager.async_send_now(dry_run=False)
