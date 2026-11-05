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
    async_remove_step_store,
    supports_step_learning,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError


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


def _record(**identity: str | None) -> dict[str, str | None]:
    """Return a stored record of a learned whole-degree step."""
    return {
        "status": "inferred",
        "device_mac": "aa:bb:cc:dd:ee:ff",
        "model": "TEST",
        "firmware_version": "1_0_0",
        "learned_at": "2026-11-01T00:00:00+00:00",
        **identity,
    }


def _coordinator() -> MagicMock:
    """Return a coordinator stand-in that records its listeners."""
    coordinator = MagicMock()
    coordinator.listeners = []

    def _add(listener):
        coordinator.listeners.append(listener)
        return MagicMock(name="unsub")

    coordinator.async_add_listener.side_effect = _add
    return coordinator


async def test_load_error_keeps_device_status(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable store leaves pydaikin's own status and is only logged."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    step = DaikinTemperatureStep(hass, entry, make_device())

    with patch.object(
        step.store, "async_load", side_effect=HomeAssistantError("broken")
    ):
        await step.async_restore()

    assert step.status == "unknown"
    assert "Unable to load Daikin step detection" in caplog.text


async def test_rejected_saved_status_is_ignored(hass: HomeAssistant) -> None:
    """A status pydaikin refuses to restore is dropped, not applied."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    step = DaikinTemperatureStep(hass, entry, make_device())
    await step.store.async_save(_record())

    def _refuse(self: DaikinBRP069, value: str) -> None:
        raise ValueError(value)

    refusing = property(lambda self: "unknown", _refuse)
    with patch.object(DaikinBRP069, "target_temperature_step_status", refusing):
        await step.async_restore()
        assert step.status == "unknown"

    async with step.async_set_context():
        pass
    assert (await step.store.async_load())["status"] == "inferred"


async def test_learning_replaces_record_of_previous_device(
    hass: HomeAssistant,
) -> None:
    """A result learned now replaces a record that belongs to other firmware."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device(firmware=None)
    step = DaikinTemperatureStep(hass, entry, device)
    await step.store.async_save(_record())
    await step.async_restore()
    assert step.status == "inferred"

    device.values["ver"] = "2_0_0"
    async with step.async_set_context():
        device.target_temperature_step_status = "confirmed"

    record = await step.store.async_load()
    assert record["status"] == "confirmed"
    assert record["firmware_version"] == "2_0_0"
    assert step.status == "confirmed"


async def test_identity_listener_invalidates_once(hass: HomeAssistant) -> None:
    """Late firmware info invalidates via one managed task, then stops watching."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device(firmware=None)
    step = DaikinTemperatureStep(hass, entry, device)
    await step.store.async_save(_record())
    await step.async_restore()
    coordinator = _coordinator()
    step.bind_coordinator(coordinator)

    step.watch_identity()
    step.watch_identity()
    assert len(coordinator.listeners) == 1

    device.values["ver"] = "2_0_0"
    release = asyncio.Event()
    remove = step.store.async_remove

    async def _slow_remove() -> None:
        await release.wait()
        await remove()

    with (
        patch.object(step.store, "async_remove", side_effect=_slow_remove),
        patch.object(
            entry,
            "async_create_background_task",
            wraps=entry.async_create_background_task,
        ) as create_task,
    ):
        coordinator.listeners[0]()
        assert not step._identity_task.done()
        coordinator.listeners[0]()
        release.set()
        await hass.async_block_till_done()

    assert create_task.call_count == 1
    assert step.status == "unknown"
    assert await step.store.async_load() is None
    coordinator.async_update_listeners.assert_called_once()
    assert step._unsub_identity is None


async def test_identity_listener_completes_record(hass: HomeAssistant) -> None:
    """Metadata that arrives later completes the record and ends the watch."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device(firmware=None)
    step = DaikinTemperatureStep(hass, entry, device)
    await step.store.async_save(_record(firmware_version=None))
    await step.async_restore()
    coordinator = _coordinator()
    step.bind_coordinator(coordinator)
    step.watch_identity()

    coordinator.listeners[0]()
    await hass.async_block_till_done()
    assert step._unsub_identity is not None

    device.values["ver"] = "1_0_0"
    coordinator.listeners[0]()
    await hass.async_block_till_done()

    assert (await step.store.async_load())["firmware_version"] == "1_0_0"
    assert step.status == "inferred"
    assert step._unsub_identity is None


async def test_close_cancels_identity_task_and_blocks_writes(
    hass: HomeAssistant,
) -> None:
    """Unloading cancels pending metadata work and refuses later commands."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device(firmware=None)
    step = DaikinTemperatureStep(hass, entry, device)
    await step.store.async_save(_record())
    await step.async_restore()
    coordinator = _coordinator()
    step.bind_coordinator(coordinator)
    step.watch_identity()
    blocked = asyncio.Event()

    async def _hang() -> None:
        blocked.set()
        await asyncio.Event().wait()

    device.values["ver"] = "2_0_0"
    with patch.object(step.store, "async_remove", side_effect=_hang):
        coordinator.listeners[0]()
        await blocked.wait()
        await step.async_close()

    assert step._identity_task.cancelled()
    with pytest.raises(HomeAssistantError):
        async with step.async_set_context():
            pass
    with pytest.raises(DaikinStepResetError):
        await step.async_reset()


async def test_remove_store_error_is_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Removing the entry never fails on a storage error."""
    entry = MockConfigEntry(domain=DOMAIN)
    with patch(
        "homeassistant.helpers.storage.Store.async_remove",
        side_effect=OSError("read-only"),
    ):
        await async_remove_step_store(hass, entry)
    assert "Unable to remove Daikin step storage" in caplog.text


async def test_failed_invalidation_is_retried(hass: HomeAssistant) -> None:
    """A stale record that cannot be removed keeps the watch for a retry."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device(firmware=None)
    step = DaikinTemperatureStep(hass, entry, device)
    await step.store.async_save(_record())
    await step.async_restore()
    coordinator = _coordinator()
    step.bind_coordinator(coordinator)
    step.watch_identity()

    device.values["ver"] = "2_0_0"
    with patch.object(step.store, "async_remove", side_effect=OSError("busy")):
        coordinator.listeners[0]()
        await hass.async_block_till_done()
    assert step.status == "unknown"
    assert step._unsub_identity is not None

    coordinator.listeners[0]()
    await hass.async_block_till_done()
    assert await step.store.async_load() is None
    assert step._unsub_identity is None


async def test_same_status_is_saved_only_when_identity_grows(
    hass: HomeAssistant,
) -> None:
    """Repeating a learned status writes nothing unless metadata was added."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device(firmware=None)
    step = DaikinTemperatureStep(hass, entry, device)
    async with step.async_set_context():
        device.target_temperature_step_status = "inferred"

    with patch.object(step.store, "async_save") as save:
        async with step.async_set_context():
            pass
    save.assert_not_called()

    device.values["ver"] = "1_0_0"
    async with step.async_set_context():
        pass
    assert (await step.store.async_load())["firmware_version"] == "1_0_0"


async def test_status_change_keeps_known_identity(hass: HomeAssistant) -> None:
    """A new status keeps stored fields the device does not report right now."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device()
    step = DaikinTemperatureStep(hass, entry, device)
    async with step.async_set_context():
        device.target_temperature_step_status = "inferred"

    del device.values["ver"]
    async with step.async_set_context():
        device.target_temperature_step_status = "confirmed"

    record = await step.store.async_load()
    assert record["status"] == "confirmed"
    assert record["firmware_version"] == "1_0_0"


async def test_listener_without_changes_does_nothing(hass: HomeAssistant) -> None:
    """An update without new metadata starts no task and keeps watching."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device(firmware=None)
    step = DaikinTemperatureStep(hass, entry, device)
    await step.store.async_save(_record(firmware_version=None))
    await step.async_restore()
    coordinator = _coordinator()
    step.bind_coordinator(coordinator)
    step.watch_identity()

    with patch.object(entry, "async_create_background_task") as create_task:
        coordinator.listeners[0]()
    create_task.assert_not_called()
    assert step._unsub_identity is not None


async def test_listener_stops_when_nothing_is_left_to_watch(
    hass: HomeAssistant,
) -> None:
    """The watch ends once metadata matches, and after a reset."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = make_device(firmware=None)
    step = DaikinTemperatureStep(hass, entry, device)
    await step.store.async_save(_record())
    await step.async_restore()
    coordinator = _coordinator()
    step.bind_coordinator(coordinator)
    step.watch_identity()

    device.values["ver"] = "1_0_0"
    coordinator.listeners[0]()
    assert step._unsub_identity is None

    step.watch_identity()
    assert len(coordinator.listeners) == 1
    await step.async_reset()
    coordinator.listeners[0]()
    assert step._unsub_identity is None
