"""Constants for the Perm Energosbyt integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

DOMAIN = "permenergosbyt"

CONF_ACCOUNT = "account"
CONF_T1_ENTITY = "t1_entity_id"
CONF_T2_ENTITY = "t2_entity_id"

DEFAULT_T1_ENTITY = "sensor.energy_t1_sensor"
DEFAULT_T2_ENTITY = "sensor.energy_t2_sensor"

# Schedule options (day-of-month + time). Stored in the config entry's
# options (not data), editable later via the options flow.
CONF_SCHEDULE_DAY = "schedule_day"
CONF_SCHEDULE_HOUR = "schedule_hour"
CONF_SCHEDULE_MINUTE = "schedule_minute"

DEFAULT_SCHEDULE_DAY = 20
DEFAULT_SCHEDULE_HOUR = 9
DEFAULT_SCHEDULE_MINUTE = 0

# Retry policy for the scheduled (automatic) send, agreed with the user:
# on the scheduled day, try RETRY_ATTEMPTS_PER_DAY times, RETRY_INTERVAL
# apart; if all fail, try the same pattern again the next day; if that also
# fails, give up and notify. A manual send (service/button) never retries.
RETRY_ATTEMPTS_PER_DAY = 3
RETRY_INTERVAL_HOURS = 2
RETRY_MAX_DAYS = 2
TOTAL_CAMPAIGN_ATTEMPTS = RETRY_ATTEMPTS_PER_DAY * RETRY_MAX_DAYS

SERVICE_SEND_READINGS = "send_readings"
ATTR_DRY_RUN = "dry_run"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

NOTIFICATION_ID_FAILURE = f"{DOMAIN}_send_failed"


def resolved_schedule(entry: ConfigEntry) -> tuple[int, int, int]:
    """Return (day, hour, minute) for the entry, applying defaults."""
    return (
        entry.options.get(CONF_SCHEDULE_DAY, DEFAULT_SCHEDULE_DAY),
        entry.options.get(CONF_SCHEDULE_HOUR, DEFAULT_SCHEDULE_HOUR),
        entry.options.get(CONF_SCHEDULE_MINUTE, DEFAULT_SCHEDULE_MINUTE),
    )


def resolved_tariff_entities(entry: ConfigEntry) -> tuple[str, str]:
    """Return (t1_entity_id, t2_entity_id) for the entry, applying defaults."""
    return (
        entry.options.get(CONF_T1_ENTITY, DEFAULT_T1_ENTITY),
        entry.options.get(CONF_T2_ENTITY, DEFAULT_T2_ENTITY),
    )


def device_info(entry: ConfigEntry) -> DeviceInfo:
    """Shared device grouping for every entity of one лицевой счёт."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Пермэнергосбыт {entry.data[CONF_ACCOUNT]}",
        manufacturer="ПАО Пермэнергосбыт",
    )
