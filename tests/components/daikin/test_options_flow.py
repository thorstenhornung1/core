"""Tests for the temperature precision options flow of Daikin."""

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from homeassistant.components.climate import ATTR_TARGET_TEMP_STEP
from homeassistant.components.daikin.const import DOMAIN, KEY_MAC
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from .conftest import (
    TEMPERATURE_STEP_STORAGE_KEY,
    ZoneDevice,
    async_setup_daikin,
    configure_temperature_step,
    temperature_step_record,
)

CLIMATE_ENTITY_ID = "climate.daikin_test"


def _assert_no_device_command(zone_device: ZoneDevice) -> None:
    """Assert that nothing was written to the device."""
    for setter in (
        zone_device.set,
        zone_device.set_zone,
        zone_device.set_holiday,
        zone_device.set_advanced_mode,
        zone_device.set_streamer,
    ):
        setter.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "step_id"),
    [
        pytest.param("unknown", "not_verified", id="unknown"),
        pytest.param(None, "unavailable", id="unavailable"),
    ],
)
async def test_status_without_learned_precision(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    issue_registry: ir.IssueRegistry,
    status: str | None,
    step_id: str,
) -> None:
    """Without a learned precision the flow only explains the status."""
    if status is None:
        del zone_device.target_temperature_step_status
    else:
        configure_temperature_step(zone_device, status)
    config_entry = await async_setup_daikin(hass, zone_device)
    assert config_entry.supports_options

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step_id
    assert result["data_schema"] is None or not result["data_schema"].schema

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {}
    assert not issue_registry.issues
    _assert_no_device_command(zone_device)


@pytest.mark.parametrize(
    ("status", "step_id"),
    [
        pytest.param("confirmed", "half_degree", id="confirmed"),
        pytest.param("inferred", "whole_degree", id="inferred"),
    ],
)
async def test_status_with_learned_precision(
    hass: HomeAssistant, zone_device: ZoneDevice, status: str, step_id: str
) -> None:
    """A learned precision is shown with reset as the only action."""
    configure_temperature_step(zone_device, status)
    config_entry = await async_setup_daikin(hass, zone_device)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == step_id
    assert result["menu_options"] == ["reset"]


async def test_reset_learned_precision(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reset asks for confirmation, then forgets the precision locally."""
    configure_temperature_step(zone_device, "unknown")
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = temperature_step_record("inferred")
    config_entry = await async_setup_daikin(hass, zone_device)
    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 1

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reset"}
    )

    # Nothing is changed before the confirmation
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reset"
    assert zone_device.target_temperature_step_status == "inferred"
    assert TEMPERATURE_STEP_STORAGE_KEY in hass_storage

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reset_successful"
    assert zone_device.target_temperature_step_status == "unknown"
    assert TEMPERATURE_STEP_STORAGE_KEY not in hass_storage
    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 0.5
    _assert_no_device_command(zone_device)

    # Learning works again after a reset
    configure_temperature_step(zone_device, "inferred")
    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage[TEMPERATURE_STEP_STORAGE_KEY]["data"]["status"] == "inferred"


async def test_options_flow_entry_not_loaded(hass: HomeAssistant) -> None:
    """The flow aborts if the device is not set up."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="001122334455",
        data={CONF_HOST: "127.0.0.1", KEY_MAC: "001122334455"},
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
