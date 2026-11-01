"""Tests for the Daikin climate and switch entities."""

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from pydaikin.exceptions import DaikinException
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    PRESET_AWAY,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.daikin.const import DOMAIN, KEY_MAC
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_HOST,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import ZoneDevice

CLIMATE_ENTITY_ID = "climate.daikin_test"


async def _async_setup_daikin(
    hass: HomeAssistant, zone_device: ZoneDevice
) -> MockConfigEntry:
    """Set up a Daikin config entry with a mocked library device."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=zone_device.mac,
        data={CONF_HOST: "127.0.0.1", KEY_MAC: zone_device.mac},
    )
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    return config_entry


async def test_target_temperature_step_follows_device(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The target temperature step follows the device after an update."""
    zone_device.target_temperature_step = 0.5
    await _async_setup_daikin(hass, zone_device)

    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 0.5

    # pydaikin falls back to whole degrees after a rejected half-degree setpoint
    zone_device.target_temperature_step = 1.0
    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 1


async def test_target_temperature_step_without_library_support(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
) -> None:
    """Whole degrees are used if pydaikin does not report a step."""
    del zone_device.target_temperature_step
    await _async_setup_daikin(hass, zone_device)

    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 1


@pytest.mark.parametrize(
    ("mode", "mode_conflict", "expected_action"),
    [
        pytest.param("hot", False, HVACAction.HEATING, id="heating"),
        pytest.param("hot", True, HVACAction.IDLE, id="heat-mode-conflict"),
        pytest.param("cool", True, HVACAction.IDLE, id="cool-mode-conflict"),
        pytest.param("off", True, HVACAction.OFF, id="off-mode-conflict"),
    ],
)
async def test_hvac_action_mode_conflict(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    mode: str,
    mode_conflict: bool,
    expected_action: HVACAction,
) -> None:
    """A multi-split mode conflict holds the unit idle despite the compressor."""
    zone_device._mode = mode
    zone_device.support_compressor_frequency = True
    zone_device.compressor_frequency = 30
    zone_device.mode_conflict = mode_conflict
    await _async_setup_daikin(hass, zone_device)

    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_HVAC_ACTION] == (
        expected_action
    )


async def test_hvac_action_without_mode_conflict_support(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
) -> None:
    """The HVAC action works if pydaikin does not report mode conflicts."""
    del zone_device.mode_conflict
    zone_device._mode = "hot"
    zone_device.support_compressor_frequency = True
    zone_device.compressor_frequency = 30
    await _async_setup_daikin(hass, zone_device)

    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_HVAC_ACTION] == (
        HVACAction.HEATING
    )


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
    await _async_setup_daikin(hass, zone_device)
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
