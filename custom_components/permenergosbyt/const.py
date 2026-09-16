"""Constants for the Perm Energosbyt integration."""

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

SERVICE_SEND_READINGS = "send_readings"
ATTR_DRY_RUN = "dry_run"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

NOTIFICATION_ID_FAILURE = f"{DOMAIN}_send_failed"
