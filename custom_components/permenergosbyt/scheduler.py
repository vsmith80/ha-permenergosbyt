"""Monthly send campaign: scheduling + retry-on-failure logic.

Agreed behaviour (see PLAN.md):
  - User sets one day-of-month + time in the integration's options.
  - On that day/time, try to send. On failure, retry up to
    RETRY_ATTEMPTS_PER_DAY times, RETRY_INTERVAL_HOURS apart.
  - If the whole day's attempts fail, try the same pattern again the next
    day (up to RETRY_MAX_DAYS days total).
  - If every attempt across every day fails, send an HA persistent
    notification.
  - A manual send (service call / button) makes exactly one attempt, raises
    immediately on failure, and never retries - but on success it cancels
    any automatic retry still pending for the current campaign.
"""

from __future__ import annotations

import logging

from homeassistant.components.persistent_notification import async_create as async_create_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_change

from .api import PermEnergosbytClient, PermEnergosbytError
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
    NOTIFICATION_ID_FAILURE,
    RETRY_ATTEMPTS_PER_DAY,
    RETRY_INTERVAL_HOURS,
    RETRY_MAX_DAYS,
)

_LOGGER = logging.getLogger(__name__)


def _campaign_offsets_hours() -> list[int]:
    """Hour offsets from campaign start for every attempt.

    E.g. with 3 attempts/day, 2h apart, over 2 days: [0, 2, 4, 24, 26, 28].
    """
    return [
        day * 24 + attempt * RETRY_INTERVAL_HOURS
        for day in range(RETRY_MAX_DAYS)
        for attempt in range(RETRY_ATTEMPTS_PER_DAY)
    ]


def _read_readings(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, float]:
    # T1/T2 sensor mapping lives in options (editable via the options flow
    # without removing/re-adding the integration), not in data.
    t1_entity = entry.options.get(CONF_T1_ENTITY, DEFAULT_T1_ENTITY)
    t2_entity = entry.options.get(CONF_T2_ENTITY, DEFAULT_T2_ENTITY)

    t1_state = hass.states.get(t1_entity)
    t2_state = hass.states.get(t2_entity)
    if t1_state is None or t2_state is None:
        raise HomeAssistantError(
            f"Не найдены сущности с показаниями: {t1_entity} / {t2_entity}"
        )

    try:
        return {"T1": float(t1_state.state), "T2": float(t2_state.state)}
    except ValueError as err:
        raise HomeAssistantError("Значения в сенсорах показаний должны быть числами") from err


class PermEnergosbytManager:
    """Owns the monthly schedule and the retry campaign for one лицевой счёт."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: PermEnergosbytClient) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client

        self._unsub_schedule: callable | None = None
        self._unsub_retry: callable | None = None
        self._campaign_offsets: list[int] = []
        self._campaign_index = 0

    # -- lifecycle -----------------------------------------------------

    def async_setup(self) -> None:
        """Start watching the clock for the configured send day/time."""
        hour = self.entry.options.get(CONF_SCHEDULE_HOUR, DEFAULT_SCHEDULE_HOUR)
        minute = self.entry.options.get(CONF_SCHEDULE_MINUTE, DEFAULT_SCHEDULE_MINUTE)

        self._unsub_schedule = async_track_time_change(
            self.hass, self._handle_time_tick, hour=hour, minute=minute, second=0
        )

    def async_unload(self) -> None:
        """Stop all pending timers for this entry."""
        if self._unsub_schedule is not None:
            self._unsub_schedule()
            self._unsub_schedule = None
        self._cancel_pending_retry()

    def _cancel_pending_retry(self) -> None:
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None

    # -- scheduled campaign ---------------------------------------------

    def _handle_time_tick(self, now) -> None:
        scheduled_day = self.entry.options.get(CONF_SCHEDULE_DAY, DEFAULT_SCHEDULE_DAY)
        if now.day != scheduled_day:
            return
        self.hass.async_create_task(self._start_campaign())

    async def _start_campaign(self) -> None:
        _LOGGER.info(
            "PermEnergosbyt: начинаем плановую отправку показаний для счёта %s",
            self.entry.data[CONF_ACCOUNT],
        )
        self._cancel_pending_retry()
        self._campaign_offsets = _campaign_offsets_hours()
        self._campaign_index = 0
        await self._run_campaign_attempt()

    async def _run_campaign_attempt(self) -> None:
        self._campaign_index += 1
        success = await self._attempt(manual=False)
        if success:
            _LOGGER.info(
                "PermEnergosbyt: показания для счёта %s успешно отправлены (попытка %d)",
                self.entry.data[CONF_ACCOUNT],
                self._campaign_index,
            )
            self._cancel_pending_retry()
            return

        if self._campaign_index < len(self._campaign_offsets):
            delay_hours = (
                self._campaign_offsets[self._campaign_index]
                - self._campaign_offsets[self._campaign_index - 1]
            )
            _LOGGER.warning(
                "PermEnergosbyt: попытка %d/%d для счёта %s не удалась, повтор через %d ч",
                self._campaign_index,
                len(self._campaign_offsets),
                self.entry.data[CONF_ACCOUNT],
                delay_hours,
            )
            self._unsub_retry = async_call_later(
                self.hass, delay_hours * 3600, self._handle_retry_timer
            )
        else:
            await self._notify_failure()

    async def _handle_retry_timer(self, _now) -> None:
        self._unsub_retry = None
        await self._run_campaign_attempt()

    async def _notify_failure(self) -> None:
        account = self.entry.data[CONF_ACCOUNT]
        _LOGGER.error(
            "PermEnergosbyt: не удалось отправить показания для счёта %s после %d попыток за %d дн.",
            account,
            len(self._campaign_offsets),
            RETRY_MAX_DAYS,
        )
        async_create_notification(
            self.hass,
            (
                f"Не удалось передать показания в Пермэнергосбыт для лицевого "
                f"счёта {account} после {len(self._campaign_offsets)} попыток. "
                "Передайте показания вручную на lk.permenergosbyt.ru или "
                "кнопкой интеграции."
            ),
            title="Пермэнергосбыт: отправка показаний не удалась",
            notification_id=f"{NOTIFICATION_ID_FAILURE}_{self.entry.entry_id}",
        )

    # -- manual send ------------------------------------------------------

    async def async_send_now(self, dry_run: bool = False) -> None:
        """Manual, single-attempt send (service call / button). Raises on failure."""
        success = await self._attempt(manual=True, dry_run=dry_run)
        if success:
            # A manual send makes any pending automatic retry for this
            # month's campaign redundant.
            self._cancel_pending_retry()
        elif not dry_run:
            raise HomeAssistantError("Не удалось отправить показания (см. журнал Home Assistant)")

    # -- shared single attempt --------------------------------------------

    async def _attempt(self, manual: bool, dry_run: bool = False) -> bool:
        account = self.entry.data[CONF_ACCOUNT]
        try:
            readings = _read_readings(self.hass, self.entry)
        except HomeAssistantError:
            if manual:
                raise
            _LOGGER.error("PermEnergosbyt: не удалось прочитать показания для счёта %s", account)
            return False

        try:
            form = await self.client.fetch_measure_form()
        except PermEnergosbytError as err:
            if manual:
                raise HomeAssistantError(str(err)) from err
            _LOGGER.error("PermEnergosbyt: ошибка получения формы для счёта %s: %s", account, err)
            return False

        if dry_run:
            _LOGGER.warning(
                "PermEnergosbyt dry_run для счёта %s: T1=%s, T2=%s (счётчик №%s) - реальная отправка НЕ выполнена",
                account,
                readings["T1"],
                readings["T2"],
                form.meter_number,
            )
            return True

        try:
            result_html = await self.client.submit_measures(form, readings)
        except PermEnergosbytError as err:
            if manual:
                raise HomeAssistantError(str(err)) from err
            _LOGGER.error("PermEnergosbyt: ошибка отправки показаний для счёта %s: %s", account, err)
            return False

        # NOTE: we have not yet performed a real submission end-to-end, so
        # the site's success/failure marker in the response is unconfirmed.
        # For now any 2xx response is treated as success; verify manually in
        # the lk.permenergosbyt.ru cabinet after the first real send and
        # tighten this check if the site returns errors with a 2xx status.
        _LOGGER.debug("PermEnergosbyt: ответ сервера после отправки для счёта %s: %s", account, result_html)
        return True
