"""Switches controlling whether automatic (scheduled/retry) sending may run.

Neither switch affects the manual send button/service - both only gate
PermEnergosbytManager._run_campaign_attempt() (fresh scheduled starts and
restart-resumed attempts).
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, device_info
from .scheduler import PermEnergosbytManager, status_signal


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: PermEnergosbytManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            PermEnergosbytPeriodBlockSwitch(entry, manager),
            PermEnergosbytAutoBlockSwitch(entry, manager),
        ]
    )


class PermEnergosbytPeriodBlockSwitch(SwitchEntity):
    """«Запрет отправки в текущем месяце».

    Turns itself on after any successful real send (manual or automatic) -
    see PermEnergosbytManager._record_result() - and clears itself once the
    calendar month changes, with no separate expiry timer. State lives in
    the manager (persisted via the campaign Store, loaded before this
    entity is even added), not in this entity's own restored state.
    """

    _attr_has_entity_name = True
    _attr_name = "Запрет отправки в текущем месяце"
    _attr_icon = "mdi:calendar-remove"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, manager: PermEnergosbytManager) -> None:
        self._entry_id = entry.entry_id
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_period_block"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        return self._manager.period_block_is_on()

    async def async_turn_on(self, **kwargs) -> None:
        await self._manager.async_set_period_block(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._manager.async_set_period_block(False)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # is_on is computed (calendar-month comparison), so nothing pushes
        # a state update by itself when the month rolls over - the shared
        # daily refresh signal (see PermEnergosbytManager.async_setup) is
        # what keeps this entity's displayed state from looking stale.
        self.async_on_remove(
            async_dispatcher_connect(self.hass, status_signal(self._entry_id), self.async_write_ha_state)
        )


class PermEnergosbytAutoBlockSwitch(SwitchEntity, RestoreEntity):
    """«Запрет автоматической отправки».

    Permanent, manual-only block - no period/expiry logic. Persists via
    this entity's own last state (unlike the period-block switch, whose
    state lives in the manager/Store). Turning it on cancels any pending
    automatic retry and clears the current campaign's progress.
    """

    _attr_has_entity_name = True
    _attr_name = "Запрет автоматической отправки"
    _attr_icon = "mdi:cancel"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, manager: PermEnergosbytManager) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_auto_send_block"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        return self._manager.auto_send_blocked

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._manager.auto_send_blocked = last_state.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._manager.auto_send_blocked = True
        await self._manager.async_reset_campaign_progress()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._manager.auto_send_blocked = False
        self.async_write_ha_state()
