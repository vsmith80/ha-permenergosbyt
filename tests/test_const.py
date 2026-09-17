"""Unit tests for const.py's pure entry->settings resolver helpers.

const.py imports two real homeassistant symbols just to type-check
(ConfigEntry, DeviceInfo) - if the real package isn't installed, minimal
stand-ins are registered first so importing const.py doesn't require the
full framework for what is otherwise plain-function logic.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from .conftest import install_minimal_homeassistant_stubs, load_component_module

install_minimal_homeassistant_stubs()
const = load_component_module("const", "const.py")


def _entry(data: dict | None = None, options: dict | None = None, entry_id: str = "entry-1"):
    """A minimal stand-in for homeassistant.config_entries.ConfigEntry -
    only .data/.options/.entry_id are ever read by const.py's helpers.
    """
    return SimpleNamespace(data=data or {}, options=options or {}, entry_id=entry_id)


# -- resolved_schedule --------------------------------------------------------


def test_resolved_schedule_defaults_when_options_empty():
    entry = _entry(options={})
    assert const.resolved_schedule(entry) == (
        const.DEFAULT_SCHEDULE_DAY,
        const.DEFAULT_SCHEDULE_HOUR,
        const.DEFAULT_SCHEDULE_MINUTE,
    )


def test_resolved_schedule_uses_configured_values():
    entry = _entry(options={
        const.CONF_SCHEDULE_DAY: 5,
        const.CONF_SCHEDULE_HOUR: 23,
        const.CONF_SCHEDULE_MINUTE: 45,
    })
    assert const.resolved_schedule(entry) == (5, 23, 45)


def test_resolved_schedule_falls_back_per_field():
    # Only the day was ever set (e.g. an older options blob); hour/minute
    # should each fall back to their own default independently.
    entry = _entry(options={const.CONF_SCHEDULE_DAY: 15})
    assert const.resolved_schedule(entry) == (
        15,
        const.DEFAULT_SCHEDULE_HOUR,
        const.DEFAULT_SCHEDULE_MINUTE,
    )


# -- resolved_tariff_entities --------------------------------------------------


def test_resolved_tariff_entities_empty_options_returns_empty_dict():
    entry = _entry(options={})
    assert const.resolved_tariff_entities(entry) == {}


def test_resolved_tariff_entities_single_tariff():
    entry = _entry(options={const.CONF_T1_ENTITY: "sensor.energy_t1_sensor"})
    assert const.resolved_tariff_entities(entry) == {"T1": "sensor.energy_t1_sensor"}


def test_resolved_tariff_entities_all_three():
    entry = _entry(options={
        const.CONF_T1_ENTITY: "sensor.t1",
        const.CONF_T2_ENTITY: "sensor.t2",
        const.CONF_T3_ENTITY: "sensor.t3",
    })
    assert const.resolved_tariff_entities(entry) == {
        "T1": "sensor.t1",
        "T2": "sensor.t2",
        "T3": "sensor.t3",
    }


def test_resolved_tariff_entities_ignores_falsy_values():
    # An unset/cleared optional tariff field should behave the same as it
    # being entirely absent from options (see config_flow.py's
    # _extract_options, which relies on this).
    entry = _entry(options={
        const.CONF_T1_ENTITY: "sensor.t1",
        const.CONF_T2_ENTITY: "",
        const.CONF_T3_ENTITY: None,
    })
    assert const.resolved_tariff_entities(entry) == {"T1": "sensor.t1"}


# -- device_info ---------------------------------------------------------------


def test_device_info_groups_by_entry_id_and_account():
    entry = _entry(data={const.CONF_ACCOUNT: "10000000001"}, entry_id="abc123")

    info = const.device_info(entry)

    assert info["identifiers"] == {(const.DOMAIN, "abc123")}
    assert info["name"] == "Пермэнергосбыт 10000000001"
    assert info["manufacturer"] == "ПАО Пермэнергосбыт"


def test_device_info_differs_per_account():
    entry_a = _entry(data={const.CONF_ACCOUNT: "111"}, entry_id="a")
    entry_b = _entry(data={const.CONF_ACCOUNT: "222"}, entry_id="b")

    assert const.device_info(entry_a) != const.device_info(entry_b)
    assert const.device_info(entry_a)["identifiers"] != const.device_info(entry_b)["identifiers"]
