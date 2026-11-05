"""Tests for Daikin target temperature step persistence."""

import asyncio
from unittest.mock import MagicMock, patch

from pydaikin.daikin_airbase import DaikinAirBase
from pydaikin.daikin_brp069 import DaikinBRP069
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.daikin.const import DOMAIN
from homeassistant.components.daikin.temperature_step import (
    DaikinStepResetError,
    DaikinTemperatureStep,
    _valid_mac,
    supports_step_learning,
)
from homeassistant.core import HomeAssistant


def make_device(*, firmware: str | None = "1_0_0") -> DaikinBRP069:
    """Build a real pydaikin appliance without network communication."""
    device = DaikinBRP069("192.0.2.12", session=MagicMock())
    device.values.update({"mac": "aabbccddeeff", "model": "TEST"})
    if firmware is not None:
        device.values["ver"] = firmware
    return device


def test_mac_validation() -> None:
    """Normalize a real MAC but never accept IPs or missing values."""
    assert _valid_mac(None) is None
    assert _valid_mac("192.0.2.12") is None
    assert _valid_mac("zz:bb:cc:dd:ee:ff") is None
    assert _valid_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"


def test_airbase_does_not_learn_step() -> None:
    """AirBase inherits the status property but has a fixed whole-degree step."""
    brp = make_device()
    airbase = DaikinAirBase("192.0.2.13", session=MagicMock())
    assert supports_step_learning(brp)
    assert hasattr(airbase, "target_temperature_step_status")
    assert not supports_step_learning(airbase)
    assert airbase.target_temperature_step == 1.0


async def test_save_and_restore(hass: HomeAssistant) -> None:
    """Learned whole-degree fallback persists across fresh appliance instances."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device()
    step = DaikinTemperatureStep(hass, entry, device)

    async with step.async_set_context():
        device.target_temperature_step_status = "inferred"

    record = await step.store.async_load()
    assert record["status"] == "inferred"
    assert record["device_mac"] == "aa:bb:cc:dd:ee:ff"
    assert record["firmware_version"] == "1_0_0"
    assert record["learned_at"]

    fresh_device = make_device()
    fresh_step = DaikinTemperatureStep(hass, entry, fresh_device)
    await fresh_step.async_restore()
    assert fresh_step.status == "inferred"
    assert fresh_device.target_temperature_step == 1.0

    async with fresh_step.async_set_context():
        fresh_device.target_temperature_step_status = "confirmed"
    assert (await fresh_step.store.async_load())["status"] == "confirmed"
    assert fresh_device.target_temperature_step == 0.5


async def test_reset_removes_store(hass: HomeAssistant) -> None:
    """A successful reset changes memory only after storage deletion."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device()
    step = DaikinTemperatureStep(hass, entry, device)
    async with step.async_set_context():
        device.target_temperature_step_status = "inferred"

    await step.async_reset()
    assert device.target_temperature_step_status == "unknown"
    assert await step.store.async_load() is None
    assert device.target_temperature_step == 0.5


async def test_failed_reset_preserves_status(hass: HomeAssistant) -> None:
    """A failed explicit reset must never report success."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device()
    step = DaikinTemperatureStep(hass, entry, device)
    async with step.async_set_context():
        device.target_temperature_step_status = "inferred"

    with (
        patch.object(step.store, "async_remove", side_effect=OSError("disk error")),
        pytest.raises(DaikinStepResetError),
    ):
        await step.async_reset()
    assert step.status == "inferred"


async def test_failed_save_isolated(hass: HomeAssistant) -> None:
    """Storage errors do not turn a successful device command into an error."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    step = DaikinTemperatureStep(hass, entry, make_device())

    with patch.object(step.store, "async_save", side_effect=OSError("disk error")):
        async with step.async_set_context():
            step.device.target_temperature_step_status = "confirmed"
    assert step.status == "confirmed"


async def test_late_identity_enrichment(hass: HomeAssistant) -> None:
    """Metadata learned after startup fills the persistent fingerprint."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device(firmware=None)
    step = DaikinTemperatureStep(hass, entry, device)
    async with step.async_set_context():
        device.target_temperature_step_status = "inferred"
    assert (await step.store.async_load())["firmware_version"] is None

    device.values["ver"] = "1_0_0"
    await step.async_check_identity()
    assert (await step.store.async_load())["firmware_version"] == "1_0_0"
    assert step.status == "inferred"


async def test_identity_change_invalidates(hass: HomeAssistant) -> None:
    """Different known firmware invalidates a previously learned capability."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    step = DaikinTemperatureStep(hass, entry, make_device())
    async with step.async_set_context():
        step.device.target_temperature_step_status = "inferred"

    changed = DaikinTemperatureStep(hass, entry, make_device(firmware="2_0_0"))
    await changed.async_restore()
    assert changed.status == "unknown"
    assert await changed.store.async_load() is None


async def test_concurrent_reset_wins(hass: HomeAssistant) -> None:
    """Reset waits for the in-flight command's persistence to finish."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    step = DaikinTemperatureStep(hass, entry, make_device())
    started = asyncio.Event()
    finish = asyncio.Event()

    async def device_transaction() -> None:
        async with step.async_set_context():
            step.device.target_temperature_step_status = "inferred"
            started.set()
            await finish.wait()

    command = asyncio.create_task(device_transaction())
    await started.wait()
    reset = asyncio.create_task(step.async_reset())
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(command, reset)

    assert step.status == "unknown"
    assert await step.store.async_load() is None
