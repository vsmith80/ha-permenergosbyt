"""Config + options flow for the Perm Energosbyt integration."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AccountNotFoundError, PermEnergosbytClient, PermEnergosbytError
from .const import (
    CONF_ACCOUNT,
    CONF_SCHEDULE_DAY,
    CONF_SCHEDULE_HOUR,
    CONF_SCHEDULE_MINUTE,
    CONF_T1_ENTITY,
    CONF_T2_ENTITY,
    DEFAULT_SCHEDULE_DAY,
    DEFAULT_SCHEDULE_HOUR,
    DEFAULT_SCHEDULE_MINUTE,
    DEFAULT_T1_ENTITY,
    DEFAULT_T2_ENTITY,
    DOMAIN,
)

_ACCOUNT_RE = re.compile(r"^\d{10,11}$")


class PermEnergosbytConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup: ask for лицевой счёт + which sensors hold each tariff."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            account = user_input[CONF_ACCOUNT].strip()
            if not _ACCOUNT_RE.match(account):
                errors["base"] = "invalid_account"
            else:
                await self.async_set_unique_id(account)
                self._abort_if_unique_id_configured()

                session = async_get_clientsession(self.hass)
                client = PermEnergosbytClient(session, account)
                try:
                    await client.fetch_measure_form()
                except AccountNotFoundError:
                    errors["base"] = "account_not_found"
                except PermEnergosbytError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"Пермэнергосбыт {account}",
                        data={CONF_ACCOUNT: account},
                        options={
                            CONF_T1_ENTITY: user_input[CONF_T1_ENTITY],
                            CONF_T2_ENTITY: user_input[CONF_T2_ENTITY],
                            CONF_SCHEDULE_DAY: DEFAULT_SCHEDULE_DAY,
                            CONF_SCHEDULE_HOUR: DEFAULT_SCHEDULE_HOUR,
                            CONF_SCHEDULE_MINUTE: DEFAULT_SCHEDULE_MINUTE,
                        },
                    )

        entity_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNT): str,
                vol.Required(CONF_T1_ENTITY, default=DEFAULT_T1_ENTITY): entity_selector,
                vol.Required(CONF_T2_ENTITY, default=DEFAULT_T2_ENTITY): entity_selector,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> PermEnergosbytOptionsFlow:
        return PermEnergosbytOptionsFlow(config_entry)


class PermEnergosbytOptionsFlow(config_entries.OptionsFlow):
    """Lets the user change the T1/T2 sensor mapping and the send schedule
    after setup, without removing and re-adding the integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if not 1 <= user_input[CONF_SCHEDULE_DAY] <= 28:
                errors["base"] = "invalid_day"
            else:
                return self.async_create_entry(data=user_input)

        options = self._config_entry.options
        entity_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_T1_ENTITY,
                    default=options.get(CONF_T1_ENTITY, DEFAULT_T1_ENTITY),
                ): entity_selector,
                vol.Required(
                    CONF_T2_ENTITY,
                    default=options.get(CONF_T2_ENTITY, DEFAULT_T2_ENTITY),
                ): entity_selector,
                vol.Required(
                    CONF_SCHEDULE_DAY,
                    default=options.get(CONF_SCHEDULE_DAY, DEFAULT_SCHEDULE_DAY),
                ): vol.All(int, vol.Range(min=1, max=28)),
                vol.Required(
                    CONF_SCHEDULE_HOUR,
                    default=options.get(CONF_SCHEDULE_HOUR, DEFAULT_SCHEDULE_HOUR),
                ): vol.All(int, vol.Range(min=0, max=23)),
                vol.Required(
                    CONF_SCHEDULE_MINUTE,
                    default=options.get(CONF_SCHEDULE_MINUTE, DEFAULT_SCHEDULE_MINUTE),
                ): vol.All(int, vol.Range(min=0, max=59)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
