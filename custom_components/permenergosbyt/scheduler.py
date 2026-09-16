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

Campaign progress (which attempt we're on) is persisted via Store so it
survives a Home Assistant restart. It is *not* touched by an options-flow
save - async_setup() only ever replaces the schedule-watcher subscription,
never the pending retry, so editing e.g. the T1 sensor mid-retry doesn't
silently drop the rest of the month's attempts.
"""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.persistent_notification import async_create as async_create_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import PermEnergosbytClient, PermEnergosbytError
from .const import (
    CONF_ACCOUNT,
    DOMAIN,
    NOTIFICATION_ID_FAILURE,
    RETRY_ATTEMPTS_PER_DAY,
    RETRY_INTERVAL_HOURS,
    RETRY_MAX_DAYS,
    TOTAL_CAMPAIGN_ATTEMPTS,
    resolved_schedule,
    resolved_tariff_entities,
)

_LOGGER = logging.getLogger(__name__)

_CAMPAIGN_STORE_VERSION = 1


def status_signal(entry_id: str) -> str:
    """Dispatcher signal name used to announce last-send-status changes."""
    return f"{DOMAIN}_{entry_id}_status"


def _campaign_store_key(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_campaign"


async def async_remove_campaign_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete persisted campaign state for an entry (called on entry removal)."""
    await Store(hass, _CAMPAIGN_STORE_VERSION, _campaign_store_key(entry_id)).async_remove()


def _campaign_delays_hours() -> list[int]:
    """Hours to wait before each retry, in order.

    E.g. with 3 attempts/day, 2h apart, over 2 days: [2, 2, 20, 2, 2] -
    attempt 1 fires immediately when the campaign starts; each entry here
    is the gap before the *next* attempt (the day-rollover gap works out to
    24 - (RETRY_ATTEMPTS_PER_DAY-1)*RETRY_INTERVAL_HOURS automatically).
    """
    offsets = [
        day * 24 + attempt * RETRY_INTERVAL_HOURS
        for day in range(RETRY_MAX_DAYS)
        for attempt in range(RETRY_ATTEMPTS_PER_DAY)
    ]
    return [after - before for before, after in zip(offsets, offsets[1:])]


def _read_readings(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, float]:
    """Read the current value of every tariff sensor configured for this entry.

    Works for single-, two- or three-tariff accounts alike - whichever
    tariffs (T1, T2, T3) the user configured in resolved_tariff_entities().
    """
    readings: dict[str, float] = {}
    missing: list[str] = []
    for tariff, entity_id in resolved_tariff_entities(entry).items():
        state = hass.states.get(entity_id)
        if state is None:
            missing.append(entity_id)
            continue
        try:
            readings[tariff] = float(state.state)
        except ValueError as err:
            raise HomeAssistantError(
                f"Значение сенсора {entity_id} ({tariff}) должно быть числом"
            ) from err

    if missing:
        raise HomeAssistantError(f"Не найдены сущности с показаниями: {', '.join(missing)}")

    return readings


class PermEnergosbytManager:
    """Owns the monthly schedule and the retry campaign for one лицевой счёт."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: PermEnergosbytClient) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client

        self._unsub_schedule: callable | None = None
        self._unsub_retry: callable | None = None
        self._campaign_delays: list[int] = []
        self._campaign_index = 0
        self._campaign_store = Store[dict](
            hass, _CAMPAIGN_STORE_VERSION, _campaign_store_key(entry.entry_id)
        )

        # Status of the last REAL (non dry-run) send attempt, for the
        # diagnostic sensor - "never" | "success" | "failed". Broadcast via
        # the dispatcher (status_signal) rather than a bespoke callback list,
        # so listener exceptions are isolated/logged by HA core instead of
        # propagating into _attempt()/_record_result().
        self.last_status: str = "never"
        self.last_attempt_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None

    def restore_status(
        self,
        status: str | None,
        attempt_at: datetime | None,
        success_at: datetime | None,
        error: str | None,
    ) -> None:
        """Restore status from the sensor's last known state after a HA restart.

        Only applies if nothing has happened yet this session, so a restore
        racing with a real, fresh result can never clobber it.
        """
        if self.last_status != "never" or status not in ("success", "failed"):
            return
        self.last_status = status
        self.last_attempt_at = attempt_at
        self.last_success_at = success_at
        self.last_error = error

    def _record_result(self, success: bool, error: str | None = None) -> None:
        self.last_attempt_at = dt_util.now()
        self.last_status = "success" if success else "failed"
        self.last_error = None if success else error
        if success:
            self.last_success_at = self.last_attempt_at
        async_dispatcher_send(self.hass, status_signal(self.entry.entry_id))

    # -- lifecycle -----------------------------------------------------

    def async_setup(self) -> None:
        """(Re-)start watching the clock for the configured send day/time.

        Safe to call again later (e.g. after an options-flow save changes
        the hour/minute) - it only replaces its own schedule-watcher
        subscription and never touches a retry already in progress.
        """
        if self._unsub_schedule is not None:
            self._unsub_schedule()
            self._unsub_schedule = None

        _, hour, minute = resolved_schedule(self.entry)
        self._unsub_schedule = async_track_time_change(
            self.hass, self._handle_time_tick, hour=hour, minute=minute, second=0
        )

    async def async_restore_campaign(self) -> None:
        """Resume a retry campaign left in progress by a HA restart.

        We don't know how long HA was offline, so we don't try to honour
        the original wall-clock retry times - we just pick the campaign
        back up immediately and continue its remaining attempts from now.
        """
        data = await self._campaign_store.async_load()
        index = data.get("campaign_index") if data else None
        if not index:
            return
        if index >= TOTAL_CAMPAIGN_ATTEMPTS:
            await self._clear_campaign_store()
            return

        _LOGGER.warning(
            "PermEnergosbyt: возобновляем кампанию отправки для счёта %s, "
            "прерванную перезапуском Home Assistant (было выполнено %d/%d попыток)",
            self.entry.data[CONF_ACCOUNT],
            index,
            TOTAL_CAMPAIGN_ATTEMPTS,
        )
        self._campaign_delays = _campaign_delays_hours()
        self._campaign_index = index
        await self._run_campaign_attempt()

    def async_unload(self) -> None:
        """Stop everything for this entry (used only on actual unload/removal)."""
        if self._unsub_schedule is not None:
            self._unsub_schedule()
            self._unsub_schedule = None
        self._cancel_pending_retry()

    def _cancel_pending_retry(self) -> None:
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None

    async def _save_campaign_store(self) -> None:
        await self._campaign_store.async_save({"campaign_index": self._campaign_index})

    async def _clear_campaign_store(self) -> None:
        await self._campaign_store.async_remove()

    # -- scheduled campaign ---------------------------------------------

    def _handle_time_tick(self, now) -> None:
        scheduled_day, _, _ = resolved_schedule(self.entry)
        if now.day != scheduled_day:
            return
        self.hass.async_create_task(self._start_campaign())

    async def _start_campaign(self) -> None:
        _LOGGER.info(
            "PermEnergosbyt: начинаем плановую отправку показаний для счёта %s",
            self.entry.data[CONF_ACCOUNT],
        )
        self._cancel_pending_retry()
        self._campaign_delays = _campaign_delays_hours()
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
            await self._clear_campaign_store()
            return

        if self._campaign_index < TOTAL_CAMPAIGN_ATTEMPTS:
            delay_hours = self._campaign_delays[self._campaign_index - 1]
            _LOGGER.warning(
                "PermEnergosbyt: попытка %d/%d для счёта %s не удалась, повтор через %d ч",
                self._campaign_index,
                TOTAL_CAMPAIGN_ATTEMPTS,
                self.entry.data[CONF_ACCOUNT],
                delay_hours,
            )
            await self._save_campaign_store()
            self._unsub_retry = async_call_later(
                self.hass, delay_hours * 3600, self._handle_retry_timer
            )
        else:
            await self._notify_failure()
            await self._clear_campaign_store()

    async def _handle_retry_timer(self, _now) -> None:
        self._unsub_retry = None
        await self._run_campaign_attempt()

    async def _notify_failure(self) -> None:
        account = self.entry.data[CONF_ACCOUNT]
        _LOGGER.error(
            "PermEnergosbyt: не удалось отправить показания для счёта %s после %d попыток за %d дн.",
            account,
            TOTAL_CAMPAIGN_ATTEMPTS,
            RETRY_MAX_DAYS,
        )
        async_create_notification(
            self.hass,
            (
                f"Не удалось передать показания в Пермэнергосбыт для лицевого "
                f"счёта {account} после {TOTAL_CAMPAIGN_ATTEMPTS} попыток. "
                "Не вышло — попробуйте передать показания вручную здесь: "
                "[lk.permenergosbyt.ru](https://lk.permenergosbyt.ru/)."
            ),
            title="Пермэнергосбыт: отправка показаний не удалась",
            notification_id=f"{NOTIFICATION_ID_FAILURE}_{self.entry.entry_id}",
        )

    # -- manual send ------------------------------------------------------

    async def async_send_now(self, dry_run: bool = False) -> None:
        """Manual, single-attempt send (service call / button).

        _attempt(manual=True, ...) always raises on failure, so reaching
        the end of this method means the attempt succeeded.
        """
        await self._attempt(manual=True, dry_run=dry_run)
        # A successful manual send makes any pending automatic retry for
        # this month's campaign redundant.
        self._cancel_pending_retry()
        if not dry_run:
            await self._clear_campaign_store()

    # -- shared single attempt --------------------------------------------

    async def _attempt(self, manual: bool, dry_run: bool = False) -> bool:
        account = self.entry.data[CONF_ACCOUNT]
        try:
            readings = _read_readings(self.hass, self.entry)
        except HomeAssistantError as err:
            if not dry_run:
                self._record_result(False, str(err))
            if manual:
                raise
            _LOGGER.error("PermEnergosbyt: не удалось прочитать показания для счёта %s", account)
            return False

        try:
            form = await self.client.fetch_measure_form()
        except PermEnergosbytError as err:
            if not dry_run:
                self._record_result(False, str(err))
            if manual:
                raise HomeAssistantError(str(err)) from err
            _LOGGER.error("PermEnergosbyt: ошибка получения формы для счёта %s: %s", account, err)
            return False

        if dry_run:
            readings_str = ", ".join(f"{tariff}={value}" for tariff, value in sorted(readings.items()))
            _LOGGER.warning(
                "PermEnergosbyt dry_run для счёта %s: %s (счётчик №%s) - реальная отправка НЕ выполнена",
                account,
                readings_str,
                form.meter_number,
            )
            return True

        try:
            result_html = await self.client.submit_measures(form, readings)
        except PermEnergosbytError as err:
            self._record_result(False, str(err))
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
        self._record_result(True)
        return True
