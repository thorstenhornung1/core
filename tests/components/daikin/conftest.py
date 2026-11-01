"""Fixtures for Daikin tests."""

from collections.abc import Callable, Generator
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
import urllib.parse

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.daikin.const import DOMAIN, KEY_MAC
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

type ZoneDefinition = list[str | int]
type ZoneDevice = MagicMock

ENTRY_ID = "daikin_entry"
TEMPERATURE_STEP_STORAGE_KEY = f"{DOMAIN}.{ENTRY_ID}.temperature_step"


def _decode_zone_values(value: str) -> list[str]:
    """Decode a semicolon separated list into zone values."""
    return re.findall(r"[^;]+", urllib.parse.unquote(value))


def configure_zone_device(
    zone_device: ZoneDevice,
    *,
    zones: list[ZoneDefinition],
    target_temperature: float | None = 22,
    mode: str = "hot",
    heating_values: str | None = None,
    cooling_values: str | None = None,
) -> None:
    """Configure a mocked zone-capable Daikin device for a test."""
    zone_device.target_temperature = target_temperature
    zone_device.zones = zones
    zone_device._mode = mode

    encoded_zone_temperatures = ";".join(str(zone[2]) for zone in zones)
    zone_device.values = {
        "name": "Daikin Test",
        "model": "TESTMODEL",
        "ver": "1_0_0",
        "zone_name": ";".join(str(zone[0]) for zone in zones),
        "zone_onoff": ";".join(str(zone[1]) for zone in zones),
        "lztemp_h": (
            encoded_zone_temperatures if heating_values is None else heating_values
        ),
        "lztemp_c": (
            encoded_zone_temperatures if cooling_values is None else cooling_values
        ),
    }


def configure_temperature_step(
    device: ZoneDevice, status: str, *, half_degree_adapter: bool = True
) -> None:
    """Report a target temperature step status and step like a BRP069.

    As in pydaikin, the step follows the status: whole degrees once inferred,
    and always on adapters without half degrees such as AirBase.
    """
    device.target_temperature_step_status = status
    type(device).target_temperature_step = PropertyMock(
        side_effect=lambda: (
            0.5
            if half_degree_adapter
            and device.target_temperature_step_status != "inferred"
            else 1.0
        )
    )


def temperature_step_record(status: str) -> dict[str, Any]:
    """Return stored temperature step data for the mocked device."""
    return {
        "version": 1,
        "minor_version": 1,
        "key": TEMPERATURE_STEP_STORAGE_KEY,
        "data": {
            "status": status,
            "device_mac": "00:11:22:33:44:55",
            "model": "TESTMODEL",
            "firmware_version": "1_0_0",
            "last_verified": "2026-11-01T08:00:00+00:00",
        },
    }


async def async_setup_daikin(
    hass: HomeAssistant, zone_device: ZoneDevice
) -> MockConfigEntry:
    """Set up a Daikin config entry with a mocked library device."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        unique_id=zone_device.mac,
        data={CONF_HOST: "127.0.0.1", KEY_MAC: zone_device.mac},
    )
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    return config_entry


@pytest.fixture
def zone_device() -> Generator[ZoneDevice]:
    """Return a mocked zone-capable Daikin device and patch its factory."""
    device = MagicMock(name="DaikinZoneDevice")
    device.mac = "001122334455"
    device.fan_rate = []
    device.swing_modes = []
    device.support_away_mode = False
    device.support_advanced_modes = False
    device.support_fan_rate = False
    device.support_swing_mode = False
    device.support_outside_temperature = False
    device.support_energy_consumption = False
    device.support_humidity = False
    device.support_compressor_frequency = False
    device.support_demand_control = False
    device.get_demand_control = MagicMock(return_value={})
    device.compressor_frequency = 0
    device.inside_temperature = 21.0
    device.outside_temperature = 13.0
    device.humidity = 40
    device.current_total_power_consumption = 0.0
    device.last_hour_cool_energy_consumption = 0.0
    device.last_hour_heat_energy_consumption = 0.0
    device.today_energy_consumption = 0.0
    device.today_total_energy_consumption = 0.0

    configure_zone_device(device, zones=[["Living", "1", 22]])

    def _represent(key: str) -> tuple[None, list[str] | str]:
        dynamic_values: dict[str, Callable[[], list[str] | str]] = {
            "lztemp_h": lambda: _decode_zone_values(device.values["lztemp_h"]),
            "lztemp_c": lambda: _decode_zone_values(device.values["lztemp_c"]),
            "mode": lambda: device._mode,
            "f_rate": lambda: "auto",
            "f_dir": lambda: "3d",
            "en_hol": lambda: "off",
            "adv": lambda: "",
            "htemp": lambda: str(device.inside_temperature),
            "otemp": lambda: str(device.outside_temperature),
        }
        return (None, dynamic_values.get(key, lambda: "")())

    async def _set(values: dict[str, Any]) -> None:
        if mode := values.get("mode"):
            device._mode = mode

    device.represent = MagicMock(side_effect=_represent)
    device.update_status = AsyncMock()
    device.set = AsyncMock(side_effect=_set)
    device.set_zone = AsyncMock()
    device.set_holiday = AsyncMock()
    device.set_advanced_mode = AsyncMock()
    device.set_streamer = AsyncMock()

    with patch(
        "homeassistant.components.daikin.DaikinFactory",
        new=AsyncMock(return_value=device),
    ):
        yield device
