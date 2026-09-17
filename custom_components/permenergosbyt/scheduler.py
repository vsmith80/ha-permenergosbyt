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

import asyncio
from datetime import datetime, timedelta
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
    next_configured_occurrence,
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


def _period_key(moment: datetime) -> str:
    """Calendar-month key ("YYYY-MM") used to decide whether a given
    successful send/block still counts as "this period" (see
    PermEnergosbytManager.period_block_is_on).
    """
    return f"{moment.year}-{moment.month:02d}"


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


def read_tariff_readings(hass: HomeAssistant, tariff_entities: dict[str, str]) -> dict[str, float]:
    """Read the current value of each given tariff's sensor.

    Takes a plain {tariff: entity_id} mapping (see resolved_tariff_entities)
    rather than a ConfigEntry, so it can be shared between real sends here
    and config_flow.py's setup-time sensor validation, where no entry
    exists yet. Works for any number of tariffs (1-3).
    """
    readings: dict[str, float] = {}
    missing: list[str] = []
    for tariff, entity_id in tariff_entities.items():
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
        self._unsub_daily_refresh: callable | None = None
        self._unsub_retry: callable | None = None
        self._restore_task: asyncio.Task | None = None
        self._campaign_delays: list[int] = []
        self._campaign_index = 0
        self._pending_resume_index = 0
        self._campaign_store = Store[dict](
            hass, _CAMPAIGN_STORE_VERSION, _campaign_store_key(entry.entry_id)
        )

        # "Запрет отправки в текущем месяце" - self-managed period block.
        # Not a plain bool: a calendar-month key ("YYYY-MM") so it clears
        # itself once the month rolls over, with no separate expiry timer.
        # Persisted alongside campaign_index in the same Store (loaded by
        # async_load_persisted_state() before platforms are set up).
        self._block_period_key: str | None = None

        # "Запрет автоматической отправки" - permanent, manual-only switch.
        # No period logic here; the switch entity itself persists this via
        # its own RestoreEntity and writes it back on startup.
        self.auto_send_blocked: bool = False

        # Wall-clock time of the next pending automatic retry, if a
        # campaign attempt just failed and a retry is scheduled. None when
        # no retry is pending (success, final failure, or never started) -
        # used by the "Дата и время отправки запланированное" sensor to
        # show the real next automatic attempt instead of just the base
        # monthly schedule.
        self._next_retry_at: datetime | None = None

        # Readings actually submitted on the last successful real send, one
        # entry per tariff - for the "Последнее переданное значение Т*"
        # sensors. Only touched on success, never on a failed/dry_run
        # attempt.
        self.last_sent_readings: dict[str, float] = {}
        # Serializes every real send attempt (manual button/service AND
        # scheduled/resumed campaign attempts) so two can never run at once
        # - e.g. a restart-resumed attempt landing in the same tick as the
        # monthly schedule firing, or a manual send racing an in-flight
        # automatic retry, would otherwise both submit to the site
        # concurrently and could corrupt _campaign_index bookkeeping.
        self._attempt_lock = asyncio.Lock()

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

    def restore_last_success_at(self, success_at: datetime | None) -> None:
        """Narrower counterpart to restore_status(), for the dedicated
        "Дата и время последней отправки" sensor - lets it restore
        independently of whether the status sensor's own restore_status()
        has run yet. Guarded on last_success_at itself (not last_status),
        so it can't clobber a real value set either by a fresh send or by
        the other sensor's restore, regardless of which one runs first -
        HA does add entities from one async_add_entities() call in list
        order, but this doesn't have to depend on that.
        """
        if self.last_success_at is not None or success_at is None:
            return
        self.last_success_at = success_at

    async def _record_result(
        self, success: bool, error: str | None = None, readings: dict[str, float] | None = None
    ) -> None:
        self.last_attempt_at = dt_util.now()
        self.last_status = "success" if success else "failed"
        self.last_error = None if success else error
        if success:
            self.last_success_at = self.last_attempt_at
            if readings is not None:
                self.last_sent_readings = dict(readings)
            # Any successful real send (manual or automatic) blocks further
            # automatic sends for the rest of this calendar month - see
            # period_block_is_on().
            self._block_period_key = _period_key(self.last_attempt_at)
            await self._save_state()
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
        if self._unsub_daily_refresh is not None:
            self._unsub_daily_refresh()
            self._unsub_daily_refresh = None

        _, hour, minute = resolved_schedule(self.entry)
        self._unsub_schedule = async_track_time_change(
            self.hass, self._handle_time_tick, hour=hour, minute=minute, second=0
        )
        # Refreshes entities whose displayed value is computed lazily and
        # would otherwise look stale until some other event touches them -
        # the period-block switch (clears at month rollover) and the
        # "настроенное"/"запланированное" schedule sensors (roll to the
        # next occurrence once the current one is in the past). Time of
        # day is arbitrary, just needs to be once daily.
        self._unsub_daily_refresh = async_track_time_change(
            self.hass, self._handle_daily_refresh, hour=0, minute=1, second=0
        )

    async def async_load_persisted_state(self) -> None:
        """Load the period-block key and any pending campaign index from
        Store. Fast/local - awaited in __init__.py before platforms are
        set up, so switch/sensor entities see correct values immediately
        rather than racing a background restore task for them.
        """
        data = await self._campaign_store.async_load()
        self._block_period_key = data.get("block_period_key") if data else None
        self._pending_resume_index = (data.get("campaign_index") or 0) if data else 0

    def period_block_is_on(self) -> bool:
        """«Запрет отправки в текущем месяце» - true iff a real send already
        succeeded this calendar month, or the user turned this on manually.
        Self-clears once the month changes - no separate expiry timer.
        """
        return self._block_period_key == _period_key(dt_util.now())

    async def async_set_period_block(self, value: bool) -> None:
        """Manual override for the period-block switch.

        Turning it on cancels any pending automatic retry and clears the
        current campaign's progress - mirrors what a successful send
        already does, so the switch actually reflects "won't send again".
        """
        if value:
            self._block_period_key = _period_key(dt_util.now())
            await self.async_reset_campaign_progress()
        else:
            self._block_period_key = None
            await self._save_state()

    async def async_reset_campaign_progress(self) -> None:
        """Cancel a pending retry and reset the campaign counter to zero.

        Shared by: a successful send (manual or automatic), turning on
        either block switch, and the restore path abandoning a stale
        campaign it's not allowed to resume. Dispatches status_signal so
        entities showing the next planned attempt (which may have just
        changed - a cancelled retry means "запланированное" falls back to
        the base schedule) refresh immediately, not just on the daily tick.
        """
        self._cancel_pending_retry()
        self._campaign_index = 0
        await self._save_state()
        async_dispatcher_send(self.hass, status_signal(self.entry.entry_id))

    def next_planned_attempt(self) -> datetime:
        """Real next automatic attempt - a pending retry if one is due
        sooner, otherwise just the next base monthly schedule occurrence.
        """
        if self._next_retry_at is not None:
            return self._next_retry_at
        return next_configured_occurrence(self.entry, dt_util.now())

    async def async_restore_campaign(self) -> None:
        """Resume a retry campaign left in progress by a HA restart.

        We don't know how long HA was offline, so we don't try to honour
        the original wall-clock retry times - we just pick the campaign
        back up immediately and continue its remaining attempts from now.
        Whether it's actually allowed to resume (neither block switch is
        on) is decided centrally in _run_campaign_attempt().
        """
        index = self._pending_resume_index
        if not index or index >= TOTAL_CAMPAIGN_ATTEMPTS:
            if index:
                await self.async_reset_campaign_progress()
            return

        _LOGGER.warning(
            "PermEnergosbyt: возобновляем кампанию отправки для счёта %s, "
            "прерванную перезапуском Home Assistant (было выполнено %d/%d попыток)",
            self.entry.data[CONF_ACCOUNT],
            index,
            TOTAL_CAMPAIGN_ATTEMPTS,
        )
        await self._run_campaign_attempt(start_index=index)

    def async_start_restore_campaign(self) -> None:
        """Fire async_restore_campaign() as a task tracked for cancellation.

        Called from __init__.py right after platform setup. Kept as a
        tracked task (not bare hass.async_create_task) so async_unload()
        can cancel it - otherwise it could outlive a removed/reloaded
        entry and re-arm a retry timer for an account no longer tracked
        in hass.data.
        """
        self._restore_task = self.hass.async_create_task(self.async_restore_campaign())

    def async_unload(self) -> None:
        """Stop everything for this entry (used only on actual unload/removal)."""
        if self._unsub_schedule is not None:
            self._unsub_schedule()
            self._unsub_schedule = None
        if self._unsub_daily_refresh is not None:
            self._unsub_daily_refresh()
            self._unsub_daily_refresh = None
        self._cancel_pending_retry()
        if self._restore_task is not None and not self._restore_task.done():
            self._restore_task.cancel()
        self._restore_task = None

    def _cancel_pending_retry(self) -> None:
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None
        self._next_retry_at = None

    def _handle_daily_refresh(self, _now) -> None:
        async_dispatcher_send(self.hass, status_signal(self.entry.entry_id))

    def notify_state_changed(self) -> None:
        """Push a display refresh to every entity listening on status_signal.

        Needed whenever something other than a real send/campaign-reset
        changes a value they show - currently only the options-flow
        schedule change (day/hour/minute), which otherwise leaves the
        "настроенное"/"запланированное" sensors showing the old schedule
        until an unrelated event (a send, a switch toggle, the daily tick)
        happens to fire this same signal.
        """
        async_dispatcher_send(self.hass, status_signal(self.entry.entry_id))

    async def _save_state(self) -> None:
        await self._campaign_store.async_save(
            {"campaign_index": self._campaign_index, "block_period_key": self._block_period_key}
        )

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
        await self._run_campaign_attempt(start_index=0)

    async def _run_campaign_attempt(self, start_index: int | None = None) -> None:
        # Locked so a resumed (restart) attempt can never overlap a fresh
        # scheduled tick, and neither can overlap a concurrent manual send.
        # start_index (re)initializes the counter/delays *inside* the lock,
        # not before it - doing that reset in the callers instead would let
        # a fresh _start_campaign() clobber an in-flight resumed attempt's
        # index while it's awaiting the lock, corrupting the count.
        async with self._attempt_lock:
            if start_index is not None:
                account = self.entry.data[CONF_ACCOUNT]
                if self.period_block_is_on() or self.auto_send_blocked:
                    _LOGGER.info(
                        "PermEnergosbyt: автоматическая отправка для счёта %s не начата — "
                        "активен «%s»",
                        account,
                        "запрет отправки в текущем месяце"
                        if self.period_block_is_on()
                        else "запрет автоматической отправки",
                    )
                    await self.async_reset_campaign_progress()
                    return
                self._campaign_delays = _campaign_delays_hours()
                self._campaign_index = start_index
            self._campaign_index += 1
            success = await self._attempt(manual=False)
            if success:
                _LOGGER.info(
                    "PermEnergosbyt: показания для счёта %s успешно отправлены (попытка %d)",
                    self.entry.data[CONF_ACCOUNT],
                    self._campaign_index,
                )
                await self.async_reset_campaign_progress()
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
                await self._save_state()
                self._next_retry_at = dt_util.now() + timedelta(hours=delay_hours)
                self._unsub_retry = async_call_later(
                    self.hass, delay_hours * 3600, self._handle_retry_timer
                )
            else:
                await self._notify_failure()
                await self.async_reset_campaign_progress()

    async def _handle_retry_timer(self, _now) -> None:
        self._unsub_retry = None
        self._next_retry_at = None
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
        async with self._attempt_lock:
            await self._attempt(manual=True, dry_run=dry_run)
        if dry_run:
            # A dry_run is just a test - it must not disturb a real
            # campaign's pending retry, only a genuine send should.
            return
        # A successful manual send makes any pending automatic retry for
        # this month's campaign redundant.
        await self.async_reset_campaign_progress()

    # -- shared single attempt --------------------------------------------

    async def _attempt(self, manual: bool, dry_run: bool = False) -> bool:
        account = self.entry.data[CONF_ACCOUNT]
        try:
            readings = read_tariff_readings(self.hass, resolved_tariff_entities(self.entry))
        except HomeAssistantError as err:
            if not dry_run:
                await self._record_result(False, str(err))
            if manual:
                raise
            _LOGGER.error("PermEnergosbyt: не удалось прочитать показания для счёта %s", account)
            return False

        try:
            form = await self.client.fetch_measure_form()
        except PermEnergosbytError as err:
            if not dry_run:
                await self._record_result(False, str(err))
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
            await self._record_result(False, str(err))
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
        await self._record_result(True, readings=readings)
        return True
