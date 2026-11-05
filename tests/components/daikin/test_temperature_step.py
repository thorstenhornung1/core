"""Tests for storing the Daikin target temperature step."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pydaikin.daikin_airbase import DaikinAirBase
from pydaikin.daikin_brp069 import DaikinBRP069
import pytest

from homeassistant.components.daikin.temperature_step import (
    DaikinStepResetError,
    DaikinTemperatureStep,
    async_remove_temperature_step,
    supports_step_learning,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

ENTRY_ID = "entry"
KEY = f"daikin.{ENTRY_ID}.temperature_step"
RECORD = {
    "status": "inferred",
    "device_mac": "aa:bb:cc:dd:ee:ff",
    "firmware_version": "1_0_0",
}


def make_device(**values: Any) -> DaikinBRP069:
    """Build a real pydaikin appliance whose set() learns a status."""
    device = DaikinBRP069("192.0.2.12", session=MagicMock())
    device.values.update({"mac": "aabbccddeeff", "ver": "1_0_0", **values})
    return device


def learns(device: DaikinBRP069, status: str) -> None:
    """Let the next device.set() end with the given pydaikin status."""

    async def _set(settings: dict[str, Any]) -> None:
        device.target_temperature_step_status = status

    device.set = AsyncMock(side_effect=_set)


def store_record(hass_storage: dict[str, Any], data: Any) -> None:
    """Put a stored step into Home Assistant's mocked storage."""
    hass_storage[KEY] = {"version": 1, "minor_version": 1, "key": KEY, "data": data}


def stored(hass_storage: dict[str, Any]) -> Any:
    """Return the stored step, None if there is none."""
    return hass_storage.get(KEY, {}).get("data")


def test_only_half_degree_adapters_learn() -> None:
    """AirBase inherits the status property but has a fixed whole-degree step."""
    airbase = DaikinAirBase("192.0.2.13", session=MagicMock())
    assert supports_step_learning(make_device())
    assert hasattr(airbase, "target_temperature_step_status")
    assert not supports_step_learning(airbase)


async def test_learned_step_survives_a_new_device_object(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A learned step is stored and restored into the next pydaikin object."""
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    learns(device, "inferred")
    await step.async_set({"stemp": "22.5"})
    assert stored(hass_storage) == RECORD

    fresh = make_device()
    await DaikinTemperatureStep(hass, ENTRY_ID, fresh).async_restore()
    assert fresh.target_temperature_step_status == "inferred"
    assert fresh.target_temperature_step == 1.0


async def test_inferred_to_confirmed_is_stored(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A later confirmed half degree replaces a learned whole degree."""
    store_record(hass_storage, RECORD)
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    await step.async_restore()

    learns(device, "confirmed")
    await step.async_set({"stemp": "22.5"})
    assert stored(hass_storage)["status"] == "confirmed"


async def test_unchanged_status_writes_nothing(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Commands that learn nothing new cause no storage I/O."""
    store_record(hass_storage, RECORD)
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    await step.async_restore()
    learns(device, "inferred")

    with patch.object(Store, "async_save") as save:
        await step.async_set({"f_rate": "auto"})
        await step.async_retry()
    save.assert_not_called()


@pytest.mark.parametrize(
    "data",
    [
        {**RECORD, "firmware_version": "2_0_0"},
        {**RECORD, "device_mac": "11:22:33:44:55:66"},
        {**RECORD, "status": "unknown"},
        {**RECORD, "firmware_version": None},
        {"status": "inferred"},
        ["inferred"],
    ],
    ids=["firmware", "mac", "status", "type", "fields", "list"],
)
async def test_other_or_invalid_record_is_discarded(
    hass: HomeAssistant, hass_storage: dict[str, Any], data: Any
) -> None:
    """A record of another device or with an invalid schema is not restored."""
    store_record(hass_storage, data)
    device = make_device()
    await DaikinTemperatureStep(hass, ENTRY_ID, device).async_restore()

    assert device.target_temperature_step_status == "unknown"
    assert stored(hass_storage) is None


@pytest.mark.parametrize(
    "values",
    [
        {"ver": None},
        {"mac": None},
        {"mac": "192.0.2.12"},
        {"ver": "x" * 129},
        {"ver": "1_0\x00"},
    ],
    ids=["no-firmware", "no-mac", "ip-as-mac", "too-long", "control-char"],
)
async def test_incomplete_identity_neither_restores_nor_stores(
    hass: HomeAssistant, hass_storage: dict[str, Any], values: dict[str, Any]
) -> None:
    """Without a reliable fingerprint nothing is restored or stored."""
    store_record(hass_storage, RECORD)
    device = make_device(**values)
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    await step.async_restore()
    assert device.target_temperature_step_status == "unknown"
    assert stored(hass_storage) == RECORD

    learns(device, "confirmed")
    await step.async_set({"stemp": "22.5"})
    assert stored(hass_storage) == RECORD


async def test_load_error_starts_unknown(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable store is logged and the device keeps its own status."""
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    with patch.object(Store, "async_load", side_effect=HomeAssistantError("broken")):
        await step.async_restore()
    assert device.target_temperature_step_status == "unknown"
    assert "Could not load the Daikin temperature step" in caplog.text


async def test_swallowed_write_error_is_retried(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Store logs a WriteError itself; the step is saved on the next sync."""
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    learns(device, "inferred")

    with patch.object(Store, "_async_write_data", side_effect=WriteError("full")):
        await step.async_set({"stemp": "22.5"})
        await step.async_retry()
    assert device.set.await_count == 1
    assert stored(hass_storage) is None

    await step.async_retry()
    assert stored(hass_storage) == RECORD


async def test_raised_save_error_is_isolated(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OSError from saving (e.g. creating .storage) never fails the command."""
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    learns(device, "inferred")

    with patch.object(Store, "async_save", side_effect=OSError("read-only")):
        await step.async_set({"stemp": "22.5"})
    assert "Could not save the Daikin temperature step" in caplog.text

    await step.async_retry()
    assert stored(hass_storage) == RECORD


async def test_device_error_keeps_its_exception(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A failing device command raises its own error and stores nothing."""
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    device.set = AsyncMock(side_effect=ValueError("rejected"))

    with pytest.raises(ValueError, match="rejected"):
        await step.async_set({"stemp": "17.5"})
    assert stored(hass_storage) is None


async def test_save_while_stopping_is_not_verified(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A save queued for the final write is retried, not trusted."""
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    learns(device, "inferred")

    hass.set_state(CoreState.stopping)
    with patch.object(Store, "async_load") as load:
        await step.async_set({"stemp": "22.5"})
    load.assert_not_called()

    hass.set_state(CoreState.running)
    await step.async_retry()
    assert stored(hass_storage) == RECORD


async def test_reset(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """A reset deletes the store first and then forgets the status."""
    store_record(hass_storage, RECORD)
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    await step.async_restore()

    with (
        patch.object(Store, "async_remove", side_effect=OSError("read-only")),
        pytest.raises(DaikinStepResetError),
    ):
        await step.async_reset()
    assert device.target_temperature_step_status == "inferred"

    await step.async_reset()
    assert device.target_temperature_step_status == "unknown"
    assert stored(hass_storage) is None


async def test_reset_waits_for_running_command(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A reset during a command runs after its save and wins."""
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _set(settings: dict[str, Any]) -> None:
        device.target_temperature_step_status = "inferred"
        started.set()
        await finish.wait()

    device.set = AsyncMock(side_effect=_set)
    command = asyncio.create_task(step.async_set({"stemp": "22.5"}))
    await started.wait()
    reset = asyncio.create_task(step.async_reset())
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(command, reset)

    assert device.target_temperature_step_status == "unknown"
    assert stored(hass_storage) is None


async def test_closed_store_saves_nothing(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """After unloading, a late command no longer writes the store."""
    device = make_device()
    step = DaikinTemperatureStep(hass, ENTRY_ID, device)
    await step.async_close()
    learns(device, "inferred")

    await step.async_set({"stemp": "22.5"})
    assert device.set.await_count == 1
    assert stored(hass_storage) is None


async def test_remove_error_is_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Removing a config entry never fails on a storage error."""
    with patch.object(Store, "async_remove", side_effect=OSError("read-only")):
        await async_remove_temperature_step(hass, ENTRY_ID)
    assert "Could not remove the Daikin temperature step" in caplog.text


async def test_stale_record_remove_error_is_logged(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale record that cannot be removed is logged, setup continues."""
    store_record(hass_storage, {**RECORD, "firmware_version": "2_0_0"})
    device = make_device()
    with patch.object(Store, "async_remove", side_effect=OSError("read-only")):
        await DaikinTemperatureStep(hass, ENTRY_ID, device).async_restore()
    assert device.target_temperature_step_status == "unknown"
    assert "Could not remove the Daikin temperature step" in caplog.text
