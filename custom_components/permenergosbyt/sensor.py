"""Diagnostic sensor: result of the last real (non dry-run) send attempt."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ACCOUNT, DOMAIN
from .scheduler import PermEnergosbytManager

_STATE_LABELS = {
    "never": "Ещё не отправлялось",
    "success": "Успешно",
    "failed": "Ошибка",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: PermEnergosbytManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PermEnergosbytStatusSensor(entry, manager)])


class PermEnergosbytStatusSensor(SensorEntity):
    """Shows success/failure of the last real send attempt, for diagnostics.

    Ignores dry_run calls on purpose - this reflects production sends only,
    scheduled or manual.
    """

    _attr_has_entity_name = True
    _attr_name = "Статус последней отправки"
    _attr_icon = "mdi:progress-check"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, manager: PermEnergosbytManager) -> None:
        self._manager = manager
        self._unsub: callable | None = None
        self._attr_unique_id = f"{entry.entry_id}_last_send_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Пермэнергосбыт {entry.data[CONF_ACCOUNT]}",
            "manufacturer": "ПАО Пермэнергосбыт",
        }

    async def async_added_to_hass(self) -> None:
        self._unsub = self._manager.add_status_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @property
    def native_value(self) -> str:
        return _STATE_LABELS.get(self._manager.last_status, self._manager.last_status)

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "last_attempt_at": (
                self._manager.last_attempt_at.isoformat()
                if self._manager.last_attempt_at
                else None
            ),
            "last_success_at": (
                self._manager.last_success_at.isoformat()
                if self._manager.last_success_at
                else None
            ),
            "last_error": self._manager.last_error,
        }
