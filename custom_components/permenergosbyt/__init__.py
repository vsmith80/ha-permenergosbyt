"""The Perm Energosbyt (lk.permenergosbyt.ru) integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PermEnergosbytClient
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DRY_RUN,
    CONF_ACCOUNT,
    DOMAIN,
    SERVICE_SEND_READINGS,
)
from .scheduler import PermEnergosbytManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["button", "sensor"]

SEND_READINGS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one лицевой счёт as a config entry."""
    session = async_get_clientsession(hass)
    client = PermEnergosbytClient(session, entry.data[CONF_ACCOUNT])

    manager = PermEnergosbytManager(hass, entry, client)
    manager.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _async_register_services(hass)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options (schedule day/time) changed - restart the schedule watcher."""
    manager: PermEnergosbytManager = hass.data[DOMAIN][entry.entry_id]
    manager.async_unload()
    manager.async_setup()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        manager: PermEnergosbytManager = hass.data[DOMAIN].pop(entry.entry_id)
        manager.async_unload()
    return unload_ok


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SEND_READINGS):
        return

    async def _handle_send_readings(call: ServiceCall) -> None:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        manager: PermEnergosbytManager | None = hass.data.get(DOMAIN, {}).get(entry_id)
        if manager is None:
            raise HomeAssistantError(f"Не найдена запись интеграции с entry_id={entry_id}")

        await manager.async_send_now(dry_run=call.data[ATTR_DRY_RUN])

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_READINGS, _handle_send_readings, schema=SEND_READINGS_SCHEMA
    )
