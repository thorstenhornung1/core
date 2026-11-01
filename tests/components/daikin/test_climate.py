"""Tests for the target temperature step of the Daikin climate entity."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from pydaikin.exceptions import DaikinException
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.daikin.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import (
    TEMPERATURE_STEP_STORAGE_KEY,
    ZoneDevice,
    async_setup_daikin,
    configure_temperature_step,
)

CLIMATE_ENTITY_ID = "climate.daikin_test"


async def _async_set_temperature(hass: HomeAssistant, temperature: float) -> None:
    """Call `climate.set_temperature` for the Daikin climate entity."""
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: CLIMATE_ENTITY_ID, ATTR_TEMPERATURE: temperature},
        blocking=True,
    )


def _step(hass: HomeAssistant) -> float:
    """Return the published target temperature step."""
    return hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP]


@pytest.mark.parametrize(
    ("status", "half_degree_adapter", "step"),
    [
        pytest.param("unknown", True, 0.5, id="brp069-unknown"),
        pytest.param("confirmed", True, 0.5, id="brp069-confirmed"),
        pytest.param("inferred", True, 1.0, id="brp069-inferred"),
        pytest.param("unknown", False, 1.0, id="airbase"),
        pytest.param("confirmed", False, 1.0, id="confirmed-but-whole-degree-step"),
    ],
)
async def test_target_temperature_step(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    status: str,
    half_degree_adapter: bool,
    step: float,
) -> None:
    """The entity offers the step pydaikin reports for the adapter."""
    configure_temperature_step(
        zone_device, status, half_degree_adapter=half_degree_adapter
    )
    await async_setup_daikin(hass, zone_device)

    assert _step(hass) == step


async def test_target_temperature_step_without_status(
    hass: HomeAssistant, zone_device: ZoneDevice
) -> None:
    """Adapters without a step status (e.g. BRP084) use whole degrees."""
    del zone_device.target_temperature_step_status
    zone_device.target_temperature_step = 1.0
    await async_setup_daikin(hass, zone_device)

    assert _step(hass) == 1.0


async def test_whole_degree_device_shows_half_degree_target(
    hass: HomeAssistant, zone_device: ZoneDevice
) -> None:
    """A half-degree setpoint is shown as is while whole degrees are offered."""
    configure_temperature_step(zone_device, "inferred")
    zone_device.target_temperature = 22.5
    await async_setup_daikin(hass, zone_device)

    state = hass.states.get(CLIMATE_ENTITY_ID)
    assert state.attributes[ATTR_TEMPERATURE] == 22.5
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == 1.0


async def test_zone_climate_keeps_whole_degree_step(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    zone_device: ZoneDevice,
) -> None:
    """Zone climate entities keep whole degrees on a confirmed device."""
    configure_temperature_step(zone_device, "confirmed")
    await async_setup_daikin(hass, zone_device)

    zone_entity_id = entity_registry.async_get_entity_id(
        CLIMATE_DOMAIN, DOMAIN, f"{zone_device.mac}-zone0-temperature"
    )
    assert zone_entity_id is not None
    assert hass.states.get(zone_entity_id).attributes[ATTR_TARGET_TEMP_STEP] == 1
    assert _step(hass) == 0.5


async def test_confirmation_is_published_and_stored(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """An accepted half degree keeps 0.5 and is stored."""
    configure_temperature_step(zone_device, "unknown")

    async def _set(values: dict[str, Any]) -> None:
        # pydaikin wrote 22.5 and read it back
        zone_device.target_temperature = 22.5
        configure_temperature_step(zone_device, "confirmed")

    zone_device.set = AsyncMock(side_effect=_set)
    config_entry = await async_setup_daikin(hass, zone_device)
    assert _step(hass) == 0.5

    await _async_set_temperature(hass, 22.5)

    zone_device.set.assert_awaited_once_with({"stemp": "22.5"})
    state = hass.states.get(CLIMATE_ENTITY_ID)
    assert state.attributes[ATTR_TEMPERATURE] == 22.5
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == 0.5
    stored = hass_storage[TEMPERATURE_STEP_STORAGE_KEY]["data"]
    assert stored["status"] == "confirmed"
    assert stored["device_mac"] == "00:11:22:33:44:55"
    assert stored["model"] == "TESTMODEL"
    assert stored["firmware_version"] == "1_0_0"
    assert config_entry.state is ConfigEntryState.LOADED


async def test_fallback_shows_applied_setpoint(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """After a pydaikin fallback the applied setpoint is shown and stored."""
    configure_temperature_step(zone_device, "unknown")

    async def _set(values: dict[str, Any]) -> None:
        # pydaikin got PARAM NG for 23.5, then set and read back 24
        zone_device.target_temperature = 24.0
        configure_temperature_step(zone_device, "inferred")

    zone_device.set = AsyncMock(side_effect=_set)
    await async_setup_daikin(hass, zone_device)

    await _async_set_temperature(hass, 23.5)

    # Home Assistant sends the request once and leaves the fallback to pydaikin
    zone_device.set.assert_awaited_once_with({"stemp": "23.5"})
    state = hass.states.get(CLIMATE_ENTITY_ID)
    assert state.attributes[ATTR_TEMPERATURE] == 24.0
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == 1.0
    assert hass_storage[TEMPERATURE_STEP_STORAGE_KEY]["data"]["status"] == "inferred"


async def test_failed_write_changes_nothing(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """A failed write propagates and neither state nor storage change."""
    configure_temperature_step(zone_device, "unknown")
    zone_device.set = AsyncMock(side_effect=DaikinException("ret=PARAM NG"))
    await async_setup_daikin(hass, zone_device)

    with pytest.raises(DaikinException):
        await _async_set_temperature(hass, 23.5)

    zone_device.set.assert_awaited_once()
    state = hass.states.get(CLIMATE_ENTITY_ID)
    assert state.attributes[ATTR_TEMPERATURE] == 22
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == 0.5
    assert TEMPERATURE_STEP_STORAGE_KEY not in hass_storage


async def test_learned_status_survives_failed_poll(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A status learned before a failing poll is still stored."""
    configure_temperature_step(zone_device, "unknown")
    await async_setup_daikin(hass, zone_device)

    configure_temperature_step(zone_device, "confirmed")
    zone_device.update_status = AsyncMock(side_effect=DaikinException("timeout"))
    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass_storage[TEMPERATURE_STEP_STORAGE_KEY]["data"]["status"] == "confirmed"


async def test_status_change_on_poll(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A status change reaches the state with the next poll, stored only once."""
    configure_temperature_step(zone_device, "unknown")
    await async_setup_daikin(hass, zone_device)

    configure_temperature_step(zone_device, "inferred")
    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert _step(hass) == 1.0
    stored = hass_storage[TEMPERATURE_STEP_STORAGE_KEY]
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = {**stored, "marker": True}

    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Unchanged status: no further write
    assert hass_storage[TEMPERATURE_STEP_STORAGE_KEY]["marker"]
