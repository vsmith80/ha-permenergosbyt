"""Minimal client for the lk.permenergosbyt.ru "measure_without_auth" flow.

Reverse-engineered from the public web form at
https://lk.permenergosbyt.ru/personal/measure_without_auth (no login required,
identified by лицевой счёт only). There is no documented/stable API - this
talks to the same internal endpoint the web form itself uses and mimics its
exact request shape. It may break if the site changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_URL = "https://lk.permenergosbyt.ru/bb/ShowProc2/web.lk_redirect_measure_without_auth"

_REQUEST_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://lk.permenergosbyt.ru/personal/measure_without_auth",
}

_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')

_ROW_RE = re.compile(
    r'<tr class="measure_input\d+">.*?<td[^>]*>\((?P<tariff>T\d)\)\s*(?P<label>[^<]+)</td>'
    r'.*?id="prev\d+"[^>]*>(?P<prev>[0-9.]+)</td>'
    r'.*?name="(?P<field>term[0-9a-zA-Z]+)"',
    re.IGNORECASE | re.DOTALL,
)
_ADDRESS_RE = re.compile(r"<strong>Адрес:\s*</strong>\s*([^<]+)</p>")
_METER_TYPE_RE = re.compile(r'<b class="type_name">([^<]+)</td>')
_METER_NUMBER_RE = re.compile(r"№(?P<number>\d+)")


class PermEnergosbytError(Exception):
    """Base error for this integration."""


class AccountNotFoundError(PermEnergosbytError):
    """Raised when the lицевой счёт could not be resolved on the site."""


@dataclass
class TariffReading:
    """A single tariff line (e.g. T1/день) found on the measure form."""

    tariff: str
    label: str
    field_name: str
    previous_value: float


@dataclass
class MeasureFormData:
    """Parsed result of the 'measure_form' step."""

    address: str
    meter_type: str
    meter_number: str
    tariffs: list[TariffReading] = field(default_factory=list)
    # Every other named, non-file, non-checkbox <input> found in the form,
    # replayed as-is when submitting (account_id, action, totalSum, ...).
    hidden_fields: dict[str, str] = field(default_factory=dict)


def _parse_tag_attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in _ATTR_RE.finditer(tag)}


def _parse_measure_form(html: str) -> MeasureFormData:
    if "account_id" not in html:
        raise AccountNotFoundError(
            "Лицевой счёт не найден, либо сайт вернул неожиданный ответ"
        )

    hidden_fields: dict[str, str] = {}
    for tag_match in _INPUT_TAG_RE.finditer(html):
        attrs = _parse_tag_attrs(tag_match.group(0))
        name = attrs.get("name")
        if not name:
            continue
        input_type = attrs.get("type", "text").lower()
        if input_type in ("file", "checkbox"):
            continue
        if name.startswith("term") or name.startswith("chkterm"):
            continue
        hidden_fields[name] = attrs.get("value", "")

    tariffs = [
        TariffReading(
            tariff=m.group("tariff"),
            label=m.group("label").strip(),
            field_name=m.group("field"),
            previous_value=float(m.group("prev")),
        )
        for m in _ROW_RE.finditer(html)
    ]

    if not tariffs:
        raise AccountNotFoundError(
            "Не удалось найти счётчики в ответе сайта для этого лицевого счёта"
        )

    address_match = _ADDRESS_RE.search(html)
    meter_type_match = _METER_TYPE_RE.search(html)
    meter_number_match = _METER_NUMBER_RE.search(html)

    return MeasureFormData(
        address=address_match.group(1).strip() if address_match else "",
        meter_type=meter_type_match.group(1).strip() if meter_type_match else "",
        meter_number=meter_number_match.group("number") if meter_number_match else "",
        tariffs=tariffs,
        hidden_fields=hidden_fields,
    )


class PermEnergosbytClient:
    """Talks to the measure_without_auth endpoint for a single account."""

    def __init__(self, session: aiohttp.ClientSession, account: str) -> None:
        self._session = session
        self._account = account

    async def fetch_measure_form(self) -> MeasureFormData:
        """Look up the account and return its meters/tariffs/previous readings."""
        payload = {"action": "measure_form", "account": self._account}
        try:
            async with self._session.post(
                API_URL, data=payload, headers=_REQUEST_HEADERS
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            # TimeoutError (asyncio.TimeoutError) is NOT a subclass of
            # aiohttp.ClientError - a request timeout would otherwise
            # propagate uncaught past this method.
            raise PermEnergosbytError(f"Ошибка соединения с lk.permenergosbyt.ru: {err}") from err

        return _parse_measure_form(html)

    async def submit_measures(
        self, form: MeasureFormData, readings: dict[str, float]
    ) -> str:
        """Submit new readings. `readings` maps tariff ("T1"/"T2") to value.

        NOTE: the success/failure response format has not been verified against
        a real submission (we deliberately avoided doing a real, irreversible
        submission during development). Treat a non-2xx status as failure;
        for a 2xx status, check the returned HTML/log manually the first time.
        """
        by_tariff = {t.tariff: t for t in form.tariffs}
        payload = dict(form.hidden_fields)
        payload["action"] = "save_measure"

        for tariff, value in readings.items():
            tariff_info = by_tariff.get(tariff)
            if tariff_info is None:
                raise PermEnergosbytError(
                    f"Тариф {tariff} отсутствует в форме показаний для этого счётчика"
                )
            payload[tariff_info.field_name] = f"{value:.3f}"

        try:
            async with self._session.post(
                API_URL, data=payload, headers=_REQUEST_HEADERS
            ) as resp:
                resp.raise_for_status()
                return await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PermEnergosbytError(f"Ошибка отправки показаний: {err}") from err
