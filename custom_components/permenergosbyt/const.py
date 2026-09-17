"""Constants for the Perm Energosbyt integration."""

from __future__ import annotations

from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

DOMAIN = "permenergosbyt"

CONF_ACCOUNT = "account"
CONF_T1_ENTITY = "t1_entity_id"
CONF_T2_ENTITY = "t2_entity_id"
CONF_T3_ENTITY = "t3_entity_id"

DEFAULT_T1_ENTITY = "sensor.energy_t1_sensor"
DEFAULT_T2_ENTITY = "sensor.energy_t2_sensor"
# No suggested default for T3 - a third tariff is the uncommon case, so its
# picker starts empty rather than guessing a sensor name.

# T1 is always required (every meter has at least one tariff); T2/T3 are
# optional, supporting single-, two- and three-tariff meters.
TARIFF_ENTITY_CONF_KEYS: dict[str, str] = {
    "T1": CONF_T1_ENTITY,
    "T2": CONF_T2_ENTITY,
    "T3": CONF_T3_ENTITY,
}

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


def next_configured_occurrence(entry: ConfigEntry, now: datetime) -> datetime:
    """Next day/hour/minute from the schedule options, strictly after `now`.

    Pure calendar math on the datetime the caller passes in (no timezone
    handling here - `now` is expected to already be in the right zone, e.g.
    via homeassistant.util.dt.now()). schedule_day is validated elsewhere
    to be 1-28, so `.replace(day=...)` is always valid regardless of month.
    """
    day, hour, minute = resolved_schedule(entry)
    candidate = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = (
            candidate.replace(year=candidate.year + 1, month=1)
            if candidate.month == 12
            else candidate.replace(month=candidate.month + 1)
        )
    return candidate


def resolved_tariff_entities(entry: ConfigEntry) -> dict[str, str]:
    """Return {tariff_label: entity_id} for every tariff configured on this
    entry - T1 is always present, T2/T3 only if the user configured them
    (this is how single-, two- and three-tariff meters are told apart).
    """
    result: dict[str, str] = {}
    for tariff, conf_key in TARIFF_ENTITY_CONF_KEYS.items():
        entity_id = entry.options.get(conf_key)
        if entity_id:
            result[tariff] = entity_id
    return result


def device_info(entry: ConfigEntry) -> DeviceInfo:
    """Shared device grouping for every entity of one лицевой счёт."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Пермэнергосбыт {entry.data[CONF_ACCOUNT]}",
        manufacturer="ПАО Пермэнергосбыт",
    )
