"""Tests for the target temperature step of the Daikin climate entity."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from pydaikin.exceptions import DaikinException
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    PRESET_AWAY,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.components.daikin.const import DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .conftest import (
    TEMPERATURE_STEP_STORAGE_KEY,
    ZoneDevice,
    async_setup_daikin,
    configure_temperature_step,
    temperature_step_record,
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


async def test_target_temperature_step_without_detection(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """Adapters that report a status but do not detect the step use whole degrees."""
    configure_temperature_step(zone_device, "unknown", half_degree_adapter=False)
    zone_device.detects_target_temperature_step = False
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = temperature_step_record("confirmed")
    config_entry = await async_setup_daikin(hass, zone_device)

    assert _step(hass) == 1.0
    assert zone_device.target_temperature_step_status == "unknown"
    assert TEMPERATURE_STEP_STORAGE_KEY not in hass_storage
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["step_id"] == "unavailable"


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
    """A rejected write raises and neither the setpoint nor the step change."""
    configure_temperature_step(zone_device, "unknown")
    zone_device.set = AsyncMock(side_effect=DaikinException("ret=PARAM NG"))
    await async_setup_daikin(hass, zone_device)

    with pytest.raises(HomeAssistantError):
        await _async_set_temperature(hass, 17.5)

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


@pytest.mark.parametrize(
    ("domain", "service", "entity_id", "service_data", "setter"),
    [
        pytest.param(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            CLIMATE_ENTITY_ID,
            {ATTR_TEMPERATURE: 22.5},
            "set",
            id="climate-set-temperature",
        ),
        pytest.param(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            CLIMATE_ENTITY_ID,
            {ATTR_HVAC_MODE: HVACMode.COOL},
            "set",
            id="climate-set-hvac-mode",
        ),
        pytest.param(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            CLIMATE_ENTITY_ID,
            {ATTR_PRESET_MODE: PRESET_AWAY},
            "set_holiday",
            id="climate-set-preset-mode",
        ),
        pytest.param(
            CLIMATE_DOMAIN,
            SERVICE_TURN_ON,
            CLIMATE_ENTITY_ID,
            {},
            "set",
            id="climate-turn-on",
        ),
        pytest.param(
            CLIMATE_DOMAIN,
            SERVICE_TURN_OFF,
            CLIMATE_ENTITY_ID,
            {},
            "set",
            id="climate-turn-off",
        ),
        pytest.param(
            CLIMATE_DOMAIN,
            SERVICE_TURN_OFF,
            "climate.daikin_test_living_temperature",
            {},
            "set_zone",
            id="zone-climate-turn-off",
        ),
        pytest.param(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            "switch.daikin_test_power",
            {},
            "set",
            id="power-switch-turn-on",
        ),
        pytest.param(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            "switch.daikin_test_power",
            {},
            "set",
            id="power-switch-turn-off",
        ),
        pytest.param(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            "switch.daikin_test_streamer",
            {},
            "set_streamer",
            id="streamer-switch-turn-on",
        ),
        pytest.param(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            "switch.daikin_test_living",
            {},
            "set_zone",
            id="zone-switch-turn-off",
        ),
    ],
)
async def test_command_failure_raises_and_refreshes(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    domain: str,
    service: str,
    entity_id: str,
    service_data: dict[str, Any],
    setter: str,
) -> None:
    """A rejected command raises and the device state is refreshed."""
    zone_device.support_away_mode = True
    zone_device.support_advanced_modes = True
    await async_setup_daikin(hass, zone_device)
    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TEMPERATURE] == 22

    getattr(zone_device, setter).side_effect = DaikinException(
        "Daikin rejected the request: ret=PARAM NG"
    )
    # The refresh reads the actual device state back
    zone_device.target_temperature = 21
    zone_device.update_status.reset_mock()

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            domain,
            service,
            {ATTR_ENTITY_ID: entity_id, **service_data},
            blocking=True,
        )

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "command_failed"
    assert err.value.translation_placeholders == {
        "error": "Daikin rejected the request: ret=PARAM NG"
    }
    zone_device.update_status.assert_awaited_once()
    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TEMPERATURE] == 21
