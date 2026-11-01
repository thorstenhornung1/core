"""Tests for the Daikin climate and switch entities."""

from typing import Any

from pydaikin.exceptions import DaikinException
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    PRESET_AWAY,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
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
