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
    CONF_T3_ENTITY,
    DEFAULT_SCHEDULE_DAY,
    DEFAULT_SCHEDULE_HOUR,
    DEFAULT_SCHEDULE_MINUTE,
    DEFAULT_T1_ENTITY,
    DEFAULT_T2_ENTITY,
    DOMAIN,
    resolved_schedule,
    resolved_tariff_entities,
)

_ACCOUNT_RE = re.compile(r"^\d{10,11}$")

# T2/T3 have no default at all when not yet configured - see the note in
# _tariff_and_schedule_schema for why.
_OPTIONAL_TARIFF_KEYS = (CONF_T2_ENTITY, CONF_T3_ENTITY)


def _tariff_and_schedule_schema(
    t1_default: str,
    t2_default: str | None,
    t3_default: str | None,
    day_default: int,
    hour_default: int,
    minute_default: int,
) -> dict:
    """Shared T1/T2/T3 + schedule fields for both the setup and options forms.

    T2/T3 are only given a `default` when a real value is known (a fresh
    suggestion at first setup, or the entry's current value when editing) -
    EntitySelector rejects `None`, so an unconfigured optional tariff must
    stay a bare vol.Optional() with no default, which is reliably omitted
    from user_input if the user leaves it untouched.

    Returns a plain dict of schema entries so callers can merge in whatever
    else they need (e.g. the account field, setup-only) before wrapping it
    in vol.Schema.
    """
    entity_selector = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))

    def optional_entity(conf_key: str, default: str | None):
        marker = vol.Optional(conf_key, default=default) if default else vol.Optional(conf_key)
        return marker, entity_selector

    t2_key, t2_validator = optional_entity(CONF_T2_ENTITY, t2_default)
    t3_key, t3_validator = optional_entity(CONF_T3_ENTITY, t3_default)

    return {
        vol.Required(CONF_T1_ENTITY, default=t1_default): entity_selector,
        t2_key: t2_validator,
        t3_key: t3_validator,
        vol.Required(CONF_SCHEDULE_DAY, default=day_default): vol.All(int, vol.Range(min=1, max=28)),
        vol.Required(CONF_SCHEDULE_HOUR, default=hour_default): vol.All(int, vol.Range(min=0, max=23)),
        vol.Required(CONF_SCHEDULE_MINUTE, default=minute_default): vol.All(int, vol.Range(min=0, max=59)),
    }


def _extract_options(user_input: dict[str, Any]) -> dict[str, Any]:
    """Build the options dict from submitted form values.

    T2/T3 are included only when actually given a value - an untouched
    optional field is either absent from user_input or falsy, and both
    cases correctly mean "this tariff doesn't apply".
    """
    options: dict[str, Any] = {
        CONF_T1_ENTITY: user_input[CONF_T1_ENTITY],
        CONF_SCHEDULE_DAY: user_input[CONF_SCHEDULE_DAY],
        CONF_SCHEDULE_HOUR: user_input[CONF_SCHEDULE_HOUR],
        CONF_SCHEDULE_MINUTE: user_input[CONF_SCHEDULE_MINUTE],
    }
    for conf_key in _OPTIONAL_TARIFF_KEYS:
        value = user_input.get(conf_key)
        if value:
            options[conf_key] = value
    return options


def _configured_tariff_labels(user_input: dict[str, Any]) -> set[str]:
    labels = {"T1"}
    if user_input.get(CONF_T2_ENTITY):
        labels.add("T2")
    if user_input.get(CONF_T3_ENTITY):
        labels.add("T3")
    return labels


class PermEnergosbytConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup: ask for лицевой счёт + which sensors hold each tariff."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            account = user_input[CONF_ACCOUNT].strip()
            if not _ACCOUNT_RE.match(account):
                errors["base"] = "invalid_account"
            else:
                # Note: the schedule-day range is already enforced by the
                # schema below (vol.Range(min=1, max=28)) - Home Assistant
                # validates user_input against it before this method is
                # ever called again, so no separate check is needed here.
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
                    configured = _configured_tariff_labels(user_input)
                    on_site = {tariff.tariff for tariff in form.tariffs}
                    if configured != on_site:
                        errors["base"] = "tariff_mismatch"
                        description_placeholders = {
                            "site_tariffs": ", ".join(sorted(on_site)),
                            "configured_tariffs": ", ".join(sorted(configured)),
                        }
                    else:
                        return self.async_create_entry(
                            title=f"Пермэнергосбыт {account}",
                            data={CONF_ACCOUNT: account},
                            options=_extract_options(user_input),
                        )

        schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNT): str,
                **_tariff_and_schedule_schema(
                    DEFAULT_T1_ENTITY,
                    DEFAULT_T2_ENTITY,
                    None,
                    DEFAULT_SCHEDULE_DAY,
                    DEFAULT_SCHEDULE_HOUR,
                    DEFAULT_SCHEDULE_MINUTE,
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> PermEnergosbytOptionsFlow:
        return PermEnergosbytOptionsFlow(config_entry)


class PermEnergosbytOptionsFlow(config_entries.OptionsFlow):
    """Lets the user change the tariff sensor mapping and the send schedule
    after setup, without removing and re-adding the integration.

    Note: removing an already-configured T2/T3 through this form (going
    from a two/three-tariff setup back down) isn't guaranteed to work -
    clearing a pre-filled optional selector's behaviour on the frontend
    isn't something this integration relies on. If that's ever needed,
    remove and re-add the integration instead.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        # The schedule-day range is enforced by the schema below, so any
        # submitted value reaching this line is already valid - no manual
        # re-check needed (see the same note in async_step_user).
        if user_input is not None:
            return self.async_create_entry(data=_extract_options(user_input))

        current = resolved_tariff_entities(self._config_entry)
        day_default, hour_default, minute_default = resolved_schedule(self._config_entry)
        schema = vol.Schema(
            _tariff_and_schedule_schema(
                current.get("T1", DEFAULT_T1_ENTITY),
                current.get("T2"),
                current.get("T3"),
                day_default,
                hour_default,
                minute_default,
            )
        )
        return self.async_show_form(step_id="init", data_schema=schema)
