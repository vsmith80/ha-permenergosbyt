"""Config + options flow for the Perm Energosbyt integration.

Setup is two steps: (1) лицевой счёт only - we fetch the account's form
right away and read off exactly how many tariffs it has, so the user never
has to know or guess that themselves; (2) one required sensor picker per
tariff the site actually reported, plus the send schedule. This is why the
tariff-entity fields here are always vol.Required with a real (or absent)
default - there is no "optional tariff" ambiguity to handle, since the set
of tariffs is discovered, not user-entered.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AccountNotFoundError, PermEnergosbytClient, PermEnergosbytError, TariffReading
from .const import (
    CONF_ACCOUNT,
    CONF_SCHEDULE_DAY,
    CONF_SCHEDULE_HOUR,
    CONF_SCHEDULE_MINUTE,
    DEFAULT_SCHEDULE_DAY,
    DEFAULT_SCHEDULE_HOUR,
    DEFAULT_SCHEDULE_MINUTE,
    DEFAULT_T1_ENTITY,
    DEFAULT_T2_ENTITY,
    DOMAIN,
    TARIFF_ENTITY_CONF_KEYS,
    resolved_schedule,
    resolved_tariff_entities,
)

_ACCOUNT_RE = re.compile(r"^\d{10,11}$")

# Suggested defaults for the discovered tariffs' sensor pickers at fresh
# setup - purely a convenience guess, not a constraint. T3 has none: nobody
# has an established convention for a third-tariff sensor name.
_SUGGESTED_DEFAULTS = {"T1": DEFAULT_T1_ENTITY, "T2": DEFAULT_T2_ENTITY}

_ENTITY_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _tariff_schema_fields(tariff_defaults: dict[str, str | None]) -> dict:
    """One required entity picker per tariff conf-key.

    `tariff_defaults` maps a CONF_T*_ENTITY key to the entity_id to
    pre-fill (or None to leave the picker empty).
    """
    fields: dict = {}
    for conf_key, default in tariff_defaults.items():
        marker = vol.Required(conf_key, default=default) if default else vol.Required(conf_key)
        fields[marker] = _ENTITY_SELECTOR
    return fields


def _schedule_schema_fields(day_default: int, hour_default: int, minute_default: int) -> dict:
    return {
        vol.Required(CONF_SCHEDULE_DAY, default=day_default): vol.All(int, vol.Range(min=1, max=28)),
        vol.Required(CONF_SCHEDULE_HOUR, default=hour_default): vol.All(int, vol.Range(min=0, max=23)),
        vol.Required(CONF_SCHEDULE_MINUTE, default=minute_default): vol.All(int, vol.Range(min=0, max=59)),
    }


def _tariffs_summary(tariffs: list[TariffReading]) -> str:
    return ", ".join(f"{t.tariff} ({t.label})" for t in tariffs)


def _extract_options(user_input: dict[str, Any], tariff_conf_keys: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {conf_key: user_input[conf_key] for conf_key in tariff_conf_keys}
    options[CONF_SCHEDULE_DAY] = user_input[CONF_SCHEDULE_DAY]
    options[CONF_SCHEDULE_HOUR] = user_input[CONF_SCHEDULE_HOUR]
    options[CONF_SCHEDULE_MINUTE] = user_input[CONF_SCHEDULE_MINUTE]
    return options


class PermEnergosbytConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Step 1: лицевой счёт. Step 2: one sensor per discovered tariff + schedule."""

    VERSION = 1

    def __init__(self) -> None:
        self._account: str | None = None
        self._tariffs: list[TariffReading] = []

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
                    form = await client.fetch_measure_form()
                except AccountNotFoundError:
                    errors["base"] = "account_not_found"
                except PermEnergosbytError:
                    errors["base"] = "cannot_connect"
                else:
                    # api.py's _ROW_RE accepts any single-digit tariff
                    # (T0-T9), but this integration only has sensor
                    # pickers for T1-T3 - reject anything else here with
                    # a clear message instead of a KeyError deeper in
                    # async_step_tariffs.
                    if any(t.tariff not in TARIFF_ENTITY_CONF_KEYS for t in form.tariffs):
                        errors["base"] = "unsupported_tariff_count"
                    else:
                        self._account = account
                        self._tariffs = form.tariffs
                        return await self.async_step_tariffs()

        schema = vol.Schema({vol.Required(CONF_ACCOUNT): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_tariffs(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        tariff_conf_keys = [TARIFF_ENTITY_CONF_KEYS[t.tariff] for t in self._tariffs]

        if user_input is not None:
            # Note: the schedule-day range is already enforced by the
            # schema below (vol.Range(min=1, max=28)) - Home Assistant
            # validates user_input against it before this method is ever
            # called again, so no separate check is needed here.
            return self.async_create_entry(
                title=f"Пермэнергосбыт {self._account}",
                data={CONF_ACCOUNT: self._account},
                options=_extract_options(user_input, tariff_conf_keys),
            )

        tariff_defaults = {
            TARIFF_ENTITY_CONF_KEYS[t.tariff]: _SUGGESTED_DEFAULTS.get(t.tariff)
            for t in self._tariffs
        }
        schema = vol.Schema(
            {
                **_tariff_schema_fields(tariff_defaults),
                **_schedule_schema_fields(DEFAULT_SCHEDULE_DAY, DEFAULT_SCHEDULE_HOUR, DEFAULT_SCHEDULE_MINUTE),
            }
        )
        return self.async_show_form(
            step_id="tariffs",
            data_schema=schema,
            description_placeholders={"tariffs_summary": _tariffs_summary(self._tariffs)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> PermEnergosbytOptionsFlow:
        return PermEnergosbytOptionsFlow(config_entry)


class PermEnergosbytOptionsFlow(config_entries.OptionsFlow):
    """Lets the user change which sensor feeds each already-discovered
    tariff, and the send schedule, without removing and re-adding the
    integration.

    Doesn't touch which tariffs exist - that was fixed at setup from the
    site's own data and shouldn't need to change for the same real meter.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = resolved_tariff_entities(self._config_entry)
        tariff_conf_keys = [TARIFF_ENTITY_CONF_KEYS[tariff] for tariff in current]

        if user_input is not None:
            # The schedule-day range is enforced by the schema below, so
            # any submitted value reaching this line is already valid.
            return self.async_create_entry(data=_extract_options(user_input, tariff_conf_keys))

        tariff_defaults = {
            TARIFF_ENTITY_CONF_KEYS[tariff]: entity_id for tariff, entity_id in current.items()
        }
        day_default, hour_default, minute_default = resolved_schedule(self._config_entry)
        schema = vol.Schema(
            {
                **_tariff_schema_fields(tariff_defaults),
                **_schedule_schema_fields(day_default, hour_default, minute_default),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
