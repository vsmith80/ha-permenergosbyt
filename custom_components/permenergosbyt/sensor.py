"""Diagnostic sensors: status, timestamps and last submitted readings."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, device_info, next_configured_occurrence, resolved_tariff_entities
from .scheduler import PermEnergosbytManager, status_signal

_STATE_LABELS = {
    "never": "Ещё не отправлялось",
    "success": "Успешно",
    "failed": "Ошибка",
}


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value else None


def _parsed_datetime_or_none(raw: str | None):
    return dt_util.parse_datetime(raw) if raw else None


def _tariff_display(tariff: str) -> str:
    """"T1" -> "Т1" - Cyrillic Т to match the rest of the UI (strings.json)."""
    return tariff.replace("T", "Т")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: PermEnergosbytManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        PermEnergosbytStatusSensor(entry, manager),
        PermEnergosbytLastSuccessSensor(entry, manager),
        PermEnergosbytConfiguredScheduleSensor(entry, manager),
        PermEnergosbytPlannedAttemptSensor(entry, manager),
    ]
    entities.extend(
        PermEnergosbytLastReadingSensor(entry, manager, tariff)
        for tariff in resolved_tariff_entities(entry)
    )
    async_add_entities(entities)


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
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._manager.restore_status(
                status=last_state.attributes.get("status_code"),
                attempt_at=_parsed_datetime_or_none(last_state.attributes.get("last_attempt_at")),
                success_at=_parsed_datetime_or_none(last_state.attributes.get("last_success_at")),
                error=last_state.attributes.get("last_error"),
            )

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, status_signal(self._entry_id), self.async_write_ha_state
            )
        )

    @property
    def native_value(self) -> str:
        return _STATE_LABELS[self._manager.last_status]

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "status_code": self._manager.last_status,
            "last_attempt_at": _iso_or_none(self._manager.last_attempt_at),
            "last_success_at": _iso_or_none(self._manager.last_success_at),
            "last_error": self._manager.last_error,
        }


class PermEnergosbytLastSuccessSensor(SensorEntity, RestoreEntity):
    """«Дата и время последней отправки» - timestamp of the last successful
    real send. Restores via manager.restore_last_success_at(), which is
    guarded independently of PermEnergosbytStatusSensor's own restore, so
    it doesn't matter which of the two entities gets added first.
    """

    _attr_has_entity_name = True
    _attr_name = "Дата и время последней отправки"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, manager: PermEnergosbytManager) -> None:
        self._entry_id = entry.entry_id
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_last_success_at"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._manager.restore_last_success_at(_parsed_datetime_or_none(last_state.state))

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, status_signal(self._entry_id), self.async_write_ha_state
            )
        )

    @property
    def native_value(self):
        return self._manager.last_success_at


class PermEnergosbytLastReadingSensor(SensorEntity, RestoreEntity):
    """«Последнее переданное значение Т*» - the reading actually submitted
    on the last successful real send for one tariff. Updated only on
    success (see PermEnergosbytManager._record_result) - a failed or
    dry_run attempt leaves the previous value in place. No unit of
    measurement - just the raw meter reading number.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, manager: PermEnergosbytManager, tariff: str) -> None:
        self._entry_id = entry.entry_id
        self._manager = manager
        self._tariff = tariff
        self._attr_name = f"Последнее переданное значение {_tariff_display(tariff)}"
        self._attr_unique_id = f"{entry.entry_id}_last_reading_{tariff.lower()}"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if self._tariff not in self._manager.last_sent_readings:
            last_state = await self.async_get_last_state()
            if last_state is not None and last_state.state not in (None, "unknown", "unavailable"):
                try:
                    self._manager.last_sent_readings[self._tariff] = float(last_state.state)
                except ValueError:
                    pass

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, status_signal(self._entry_id), self.async_write_ha_state
            )
        )

    @property
    def native_value(self) -> float | None:
        return self._manager.last_sent_readings.get(self._tariff)


class PermEnergosbytConfiguredScheduleSensor(SensorEntity):
    """«Дата и время отправки настроенное» - next day/hour/minute from the
    schedule options, computed purely from settings - ignores whether a
    retry campaign is currently in progress (see PlannedAttemptSensor for
    that) and ignores both block switches, always showing the theoretical
    schedule.
    """

    _attr_has_entity_name = True
    _attr_name = "Дата и время отправки настроенное"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, manager: PermEnergosbytManager) -> None:
        self._entry_id = entry.entry_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_configured_schedule"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, status_signal(self._entry_id), self.async_write_ha_state
            )
        )

    @property
    def native_value(self):
        return next_configured_occurrence(self._entry, dt_util.now())


class PermEnergosbytPlannedAttemptSensor(SensorEntity):
    """«Дата и время отправки запланированное» - the real next automatic
    attempt: a pending retry's time if a campaign is currently mid-retry,
    otherwise the same value as the "настроенное" sensor. Also ignores
    both block switches, same as "настроенное".
    """

    _attr_has_entity_name = True
    _attr_name = "Дата и время отправки запланированное"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock-outline"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, manager: PermEnergosbytManager) -> None:
        self._entry_id = entry.entry_id
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_planned_attempt"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, status_signal(self._entry_id), self.async_write_ha_state
            )
        )

    @property
    def native_value(self):
        return self._manager.next_planned_attempt()
