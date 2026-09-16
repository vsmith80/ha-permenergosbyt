"""Diagnostic sensor: result of the last real (non dry-run) send attempt."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import CONF_ACCOUNT, DOMAIN
from .scheduler import PermEnergosbytManager, status_signal

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


class PermEnergosbytStatusSensor(SensorEntity, RestoreEntity):
    """Shows success/failure of the last real send attempt, for diagnostics.

    Ignores dry_run calls on purpose - this reflects production sends only,
    scheduled or manual. Survives HA restarts via RestoreEntity, since the
    manager itself only tracks status in memory for the running session.
    """

    _attr_has_entity_name = True
    _attr_name = "Статус последней отправки"
    _attr_icon = "mdi:progress-check"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, manager: PermEnergosbytManager) -> None:
        self._entry_id = entry.entry_id
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_last_send_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Пермэнергосбыт {entry.data[CONF_ACCOUNT]}",
            "manufacturer": "ПАО Пермэнергосбыт",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            status = last_state.attributes.get("status_code")
            self._manager.restore_status(
                status=status,
                attempt_at=dt_util.parse_datetime(
                    last_state.attributes.get("last_attempt_at") or ""
                ),
                success_at=dt_util.parse_datetime(
                    last_state.attributes.get("last_success_at") or ""
                ),
                error=last_state.attributes.get("last_error"),
            )

        self.async_on_unload(
            async_dispatcher_connect(
                self.hass, status_signal(self._entry_id), self.async_write_ha_state
            )
        )

    @property
    def native_value(self) -> str:
        return _STATE_LABELS.get(self._manager.last_status, self._manager.last_status)

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "status_code": self._manager.last_status,
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
