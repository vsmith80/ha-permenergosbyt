"""Unit tests for api.py: HTML parsing regexes + the HTTP client.

No Home Assistant dependency at all - api.py only imports stdlib +
aiohttp. The HTTP layer is tested against small hand-written fakes
(see _FakeSession below) rather than a library like aioresponses, which
turned out to be incompatible with the aiohttp version available here -
this way isn't left depending on that ecosystem lag, and it gives exact
control over what the fake "session" returns.

Fixture HTML files under tests/fixtures/ are derived from a real page
captured from lk.permenergosbyt.ru during development (see PROGRESS.md),
not hand-invented markup - this is what actually catches a real
regression if the site's HTML structure drifts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from .conftest import load_component_module, read_fixture

api = load_component_module("api", "api.py")


# -- fakes for the aiohttp session, no external mocking library needed ------


class _FakeResponse:
    def __init__(self, status: int = 200, text: str = "", raise_exc: Exception | None = None) -> None:
        self.status = status
        self._text = text
        self._raise_exc = raise_exc

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeSession:
    """Stands in for aiohttp.ClientSession: .post() returns a queued fake
    response (or raises, to simulate a connection failure) and records
    every call's url/kwargs for assertions.
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# -- _parse_measure_form ------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name,expected_tariffs",
    [
        ("measure_form_1_tariff.html", ["T1"]),
        ("measure_form_2_tariff.html", ["T1", "T2"]),
        ("measure_form_3_tariff.html", ["T1", "T2", "T3"]),
    ],
)
def test_parse_measure_form_tariff_counts(fixture_name, expected_tariffs):
    html = read_fixture(fixture_name)
    form = api._parse_measure_form(html)
    assert [t.tariff for t in form.tariffs] == expected_tariffs


def test_parse_measure_form_fields_from_real_site_sample():
    html = read_fixture("measure_form_2_tariff.html")
    form = api._parse_measure_form(html)

    assert form.address == "Пермский край, г. Пермь, ул. Тестовая, д. 1, кв. 1"
    assert form.meter_type == "Электроэнергия"
    assert form.meter_number == "100001"

    t1, t2 = form.tariffs
    assert t1.tariff == "T1"
    assert t1.label == "день"
    assert t1.field_name == "term99999999i2"
    assert t1.previous_value == pytest.approx(10000.000)
    assert t2.tariff == "T2"
    assert t2.label == "ночь"
    assert t2.field_name == "term99999999i3"
    assert t2.previous_value == pytest.approx(20000.000)


def test_parse_measure_form_hidden_fields_exclude_term_and_checkbox_inputs():
    html = read_fixture("measure_form_2_tariff.html")
    form = api._parse_measure_form(html)

    assert form.hidden_fields["account_id"] == "10000001"
    assert form.hidden_fields["action"] == "measures_without_auth"
    assert form.hidden_fields["is_business_proc_step4"] == "0"
    assert form.hidden_fields["totalSum"] == "0"
    # term*/chkterm* are the per-tariff reading fields that submit_measures()
    # fills in itself per request - they must not leak through as frozen
    # values captured at fetch time.
    assert not any(key.startswith("term") for key in form.hidden_fields)
    assert not any(key.startswith("chkterm") for key in form.hidden_fields)
    # The file-upload fields (photo attachment) aren't real form data either.
    assert "99999999_2" not in form.hidden_fields
    assert "ds_files_img[]" not in form.hidden_fields


def test_parse_measure_form_missing_account_id_raises_not_found():
    with pytest.raises(api.AccountNotFoundError):
        api._parse_measure_form("<html><body>показания не найдены</body></html>")


def test_parse_measure_form_no_tariff_rows_raises_not_found():
    # account_id present (so it's not the generic "bad response" case),
    # but no measure_input rows - e.g. a future markup change breaking
    # _ROW_RE, or a genuinely empty account.
    html = '<input type="hidden" id="account_id" value="123" name="account_id" />'
    with pytest.raises(api.AccountNotFoundError):
        api._parse_measure_form(html)


# -- PermEnergosbytClient.fetch_measure_form ---------------------------------


@pytest.mark.asyncio
async def test_fetch_measure_form_parses_the_response():
    html = read_fixture("measure_form_2_tariff.html")
    session = _FakeSession([_FakeResponse(status=200, text=html)])
    client = api.PermEnergosbytClient(session, "10000000001")

    form = await client.fetch_measure_form()

    assert [t.tariff for t in form.tariffs] == ["T1", "T2"]
    call = session.calls[0]
    assert call["url"] == api.API_URL
    assert call["data"] == {"action": "measure_form", "account": "10000000001"}


@pytest.mark.asyncio
async def test_fetch_measure_form_wraps_connection_errors():
    import aiohttp

    session = _FakeSession([aiohttp.ClientConnectionError("boom")])
    client = api.PermEnergosbytClient(session, "10000000001")

    with pytest.raises(api.PermEnergosbytError):
        await client.fetch_measure_form()


@pytest.mark.asyncio
async def test_fetch_measure_form_wraps_http_error_status():
    import aiohttp

    fake_request_info = SimpleNamespace(real_url=api.API_URL)
    response = _FakeResponse(
        status=502,
        raise_exc=aiohttp.ClientResponseError(fake_request_info, (), status=502),
    )
    session = _FakeSession([response])
    client = api.PermEnergosbytClient(session, "10000000001")

    with pytest.raises(api.PermEnergosbytError):
        await client.fetch_measure_form()


# -- PermEnergosbytClient.submit_measures ------------------------------------


@pytest.mark.asyncio
async def test_submit_measures_sends_only_the_requested_tariffs():
    form_html = read_fixture("measure_form_2_tariff.html")
    session = _FakeSession(
        [
            _FakeResponse(status=200, text=form_html),
            _FakeResponse(status=200, text="OK"),
        ]
    )
    client = api.PermEnergosbytClient(session, "10000000001")
    form = await client.fetch_measure_form()

    await client.submit_measures(form, {"T1": 44300.5})

    submit_call = session.calls[1]
    payload = submit_call["data"]
    assert payload["action"] == "save_measure"
    assert payload["term99999999i2"] == "44300.500"
    assert "term99999999i3" not in payload
    # Every other hidden field captured at fetch time is replayed as-is.
    assert payload["account_id"] == "10000001"
    assert payload["is_business_proc_step4"] == "0"


@pytest.mark.asyncio
async def test_submit_measures_rejects_a_tariff_not_on_the_form():
    # Single-tariff account (only T1 exists), but T2 is requested too -
    # this is exactly the "single-tariff meter" failure mode documented
    # in README.md's known limitations.
    form_html = read_fixture("measure_form_1_tariff.html")
    session = _FakeSession([_FakeResponse(status=200, text=form_html)])
    client = api.PermEnergosbytClient(session, "10000000001")
    form = await client.fetch_measure_form()

    with pytest.raises(api.PermEnergosbytError):
        await client.submit_measures(form, {"T1": 1.0, "T2": 2.0})


@pytest.mark.asyncio
async def test_submit_measures_wraps_connection_errors():
    import aiohttp

    form_html = read_fixture("measure_form_1_tariff.html")
    session = _FakeSession(
        [
            _FakeResponse(status=200, text=form_html),
            aiohttp.ClientConnectionError("boom"),
        ]
    )
    client = api.PermEnergosbytClient(session, "10000000001")
    form = await client.fetch_measure_form()

    with pytest.raises(api.PermEnergosbytError):
        await client.submit_measures(form, {"T1": 1.0})
