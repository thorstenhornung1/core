"""Define tests for the Daikin init."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
from pydaikin.exceptions import DaikinException
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from homeassistant.components.climate import ATTR_TARGET_TEMP_STEP
from homeassistant.components.daikin import update_unique_id
from homeassistant.components.daikin.const import DOMAIN, KEY_MAC
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from .conftest import (
    TEMPERATURE_STEP_STORAGE_KEY,
    ZoneDevice,
    async_setup_daikin,
    configure_temperature_step,
    temperature_step_record,
)
from .test_config_flow import HOST, MAC

CLIMATE_ENTITY_ID = "climate.daikin_test"


@pytest.fixture
def mock_daikin():
    """Mock pydaikin."""

    async def mock_daikin_factory(*args, **kwargs):
        """Mock the init function in pydaikin."""
        return Appliance

    with patch("homeassistant.components.daikin.DaikinFactory") as Appliance:
        Appliance.side_effect = mock_daikin_factory
        type(Appliance).update_status = AsyncMock()
        type(Appliance).device_ip = PropertyMock(return_value=HOST)
        type(Appliance).inside_temperature = PropertyMock(return_value=22)
        type(Appliance).target_temperature = PropertyMock(return_value=22)
        type(Appliance).zones = PropertyMock(return_value=[("Zone 1", "0", 0)])
        type(Appliance).fan_rate = PropertyMock(return_value=[])
        type(Appliance).swing_modes = PropertyMock(return_value=[])
        yield Appliance


DATA = {
    "ver": "1_1_8",
    "name": "DaikinAP00000",
    "mac": MAC,
    "model": "NOTSUPPORT",
}


INVALID_DATA = {**DATA, "name": None, "mac": HOST}


async def test_duplicate_removal(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    mock_daikin,
) -> None:
    """Test duplicate device removal."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        title=None,
        data={CONF_HOST: HOST, KEY_MAC: HOST},
    )
    config_entry.add_to_hass(hass)

    type(mock_daikin).mac = PropertyMock(return_value=HOST)
    type(mock_daikin).values = PropertyMock(return_value=INVALID_DATA)

    with patch(
        "homeassistant.components.daikin.async_migrate_unique_id", return_value=None
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)

        await hass.async_block_till_done()

        assert config_entry.unique_id != MAC

        type(mock_daikin).mac = PropertyMock(return_value=MAC)
        type(mock_daikin).values = PropertyMock(return_value=DATA)

        assert await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()

        assert (
            device_registry.async_get_device_by_connection(
                (KEY_MAC, MAC), config_entry.entry_id
            ).name
            == "DaikinAP00000"
        )

        assert (
            device_registry.async_get_device_by_connection(
                (KEY_MAC, HOST), config_entry.entry_id
            ).name
            is None
        )

        assert entity_registry.async_get("climate.daikin_127_0_0_1").unique_id == HOST
        assert entity_registry.async_get("switch.zone_1").unique_id.startswith(HOST)

        assert entity_registry.async_get("climate.daikinap00000").unique_id == MAC
        assert entity_registry.async_get(
            "switch.daikinap00000_zone_1"
        ).unique_id.startswith(MAC)

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        device_registry.async_get_device_by_connection(
            (KEY_MAC, MAC), config_entry.entry_id
        ).name
        == "DaikinAP00000"
    )

    assert entity_registry.async_get("climate.daikinap00000") is None
    assert entity_registry.async_get("switch.daikinap00000_zone_1") is None

    assert entity_registry.async_get("climate.daikin_127_0_0_1").unique_id == MAC
    assert entity_registry.async_get("switch.zone_1").unique_id.startswith(MAC)


async def test_unique_id_migrate(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    mock_daikin,
) -> None:
    """Test unique id migration."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        title=None,
        data={CONF_HOST: HOST, KEY_MAC: HOST},
    )
    config_entry.add_to_hass(hass)

    type(mock_daikin).mac = PropertyMock(return_value=HOST)
    type(mock_daikin).values = PropertyMock(return_value=INVALID_DATA)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.unique_id == HOST

    assert (
        device_registry.async_get_device_by_connection(
            (KEY_MAC, HOST), config_entry.entry_id
        ).name
        is None
    )

    entity = entity_registry.async_get("climate.daikin_127_0_0_1")
    assert entity.unique_id == HOST
    assert update_unique_id(entity, MAC) is not None

    assert entity_registry.async_get("switch.zone_1").unique_id.startswith(HOST)

    type(mock_daikin).mac = PropertyMock(return_value=MAC)
    type(mock_daikin).values = PropertyMock(return_value=DATA)

    assert config_entry.unique_id != MAC

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.unique_id == MAC

    assert (
        device_registry.async_get_device_by_connection(
            (KEY_MAC, MAC), config_entry.entry_id
        ).name
        == "DaikinAP00000"
    )

    entity = entity_registry.async_get("climate.daikin_127_0_0_1")
    assert entity.unique_id == MAC
    assert update_unique_id(entity, MAC) is None

    assert entity_registry.async_get("switch.zone_1").unique_id.startswith(MAC)


async def test_client_update_connection_error(
    hass: HomeAssistant, mock_daikin, freezer: FrozenDateTimeFactory
) -> None:
    """Test client connection error on update."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data={CONF_HOST: HOST, KEY_MAC: MAC},
    )
    config_entry.add_to_hass(hass)

    type(mock_daikin).mac = PropertyMock(return_value=MAC)
    type(mock_daikin).values = PropertyMock(return_value=DATA)

    await hass.config_entries.async_setup(config_entry.entry_id)

    assert hass.states.get("climate.daikinap00000").state != STATE_UNAVAILABLE

    type(mock_daikin).update_status.side_effect = ClientConnectionError

    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get("climate.daikinap00000").state == STATE_UNAVAILABLE

    assert mock_daikin.update_status.call_count == 2


async def test_client_connection_error(hass: HomeAssistant, mock_daikin) -> None:
    """Test client connection error on setup."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data={CONF_HOST: HOST, KEY_MAC: MAC},
    )
    config_entry.add_to_hass(hass)

    mock_daikin.side_effect = ClientConnectionError
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_timeout_error(hass: HomeAssistant, mock_daikin) -> None:
    """Test timeout error on setup."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data={CONF_HOST: HOST, KEY_MAC: MAC},
    )
    config_entry.add_to_hass(hass)

    mock_daikin.side_effect = TimeoutError
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_daikin_exception_retries(hass: HomeAssistant, mock_daikin) -> None:
    """Test that a DaikinException during setup triggers SETUP_RETRY.

    A DaikinException (e.g. "Empty values." from DaikinAirBase.init when the
    device HTTP endpoint returns an empty response) is a transient condition —
    the device is not yet ready, not a permanent configuration problem — so the
    entry must land in SETUP_RETRY rather than SETUP_ERROR.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data={CONF_HOST: HOST, KEY_MAC: MAC},
    )
    config_entry.add_to_hass(hass)

    mock_daikin.side_effect = DaikinException("Empty values.")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    ("status", "step"),
    [
        pytest.param("confirmed", 0.5, id="confirmed"),
        pytest.param("inferred", 1.0, id="inferred"),
    ],
)
async def test_stored_temperature_step_restored(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    hass_storage: dict[str, Any],
    status: str,
    step: float,
) -> None:
    """A stored classification is in place before the entity is published."""
    configure_temperature_step(zone_device, "unknown")
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = temperature_step_record(status)
    steps = []

    @callback
    def _record_first_state(event: Event[EventStateChangedData]) -> None:
        if event.data["entity_id"] == CLIMATE_ENTITY_ID and not steps:
            steps.append(event.data["new_state"].attributes[ATTR_TARGET_TEMP_STEP])

    hass.bus.async_listen(EVENT_STATE_CHANGED, _record_first_state)
    await async_setup_daikin(hass, zone_device)

    assert zone_device.target_temperature_step_status == status
    assert steps == [step]
    # Restoring is no reason to write to the device
    zone_device.set.assert_not_awaited()


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({"device_mac": "aa:bb:cc:dd:ee:ff"}, id="adapter-replaced"),
        pytest.param({"firmware_version": "1_0_1"}, id="firmware-changed"),
        pytest.param({"model": "OTHERMODEL"}, id="model-changed"),
        pytest.param({"status": "unknown"}, id="unknown-status"),
        pytest.param({"status": "half"}, id="invalid-status"),
    ],
)
async def test_stored_temperature_step_discarded(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    hass_storage: dict[str, Any],
    identity: dict[str, str],
) -> None:
    """A classification that does not match the device is discarded."""
    configure_temperature_step(zone_device, "unknown")
    record = temperature_step_record("confirmed")
    record["data"] |= identity
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = record
    config_entry = await async_setup_daikin(hass, zone_device)

    assert config_entry.state is ConfigEntryState.LOADED
    assert zone_device.target_temperature_step_status == "unknown"
    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 0.5
    assert TEMPERATURE_STEP_STORAGE_KEY not in hass_storage


async def test_stored_confirmation_rejected_by_device(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """A device that cannot offer half degrees does not take a confirmation."""
    configure_temperature_step(zone_device, "unknown", half_degree_adapter=False)
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = temperature_step_record("confirmed")
    await async_setup_daikin(hass, zone_device)

    assert zone_device.target_temperature_step_status == "unknown"
    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 1
    assert TEMPERATURE_STEP_STORAGE_KEY not in hass_storage


async def test_stored_status_refused_by_pydaikin(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """A status the pydaikin setter refuses is discarded."""
    configure_temperature_step(zone_device, "unknown")
    del zone_device.target_temperature_step_status

    def _status(*args: str) -> str:
        if args:
            raise ValueError("Invalid target temperature step status")
        return "unknown"

    type(zone_device).target_temperature_step_status = PropertyMock(side_effect=_status)
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = temperature_step_record("confirmed")
    config_entry = await async_setup_daikin(hass, zone_device)

    assert config_entry.state is ConfigEntryState.LOADED
    assert TEMPERATURE_STEP_STORAGE_KEY not in hass_storage


async def test_stored_temperature_step_without_device_support(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """A device without step status gets nothing restored."""
    del zone_device.target_temperature_step_status
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = temperature_step_record("confirmed")
    await async_setup_daikin(hass, zone_device)

    assert not hasattr(zone_device, "target_temperature_step_status")
    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 1
    assert TEMPERATURE_STEP_STORAGE_KEY not in hass_storage


async def test_stored_temperature_step_newer_version(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """Data written by a newer Home Assistant is ignored and kept."""
    configure_temperature_step(zone_device, "unknown")
    record = temperature_step_record("confirmed") | {"version": 2}
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = record
    config_entry = await async_setup_daikin(hass, zone_device)

    assert config_entry.state is ConfigEntryState.LOADED
    assert zone_device.target_temperature_step_status == "unknown"
    assert hass_storage[TEMPERATURE_STEP_STORAGE_KEY] == record


async def test_existing_installation_without_record(
    hass: HomeAssistant,
    zone_device: ZoneDevice,
    hass_storage: dict[str, Any],
    issue_registry: ir.IssueRegistry,
) -> None:
    """An entry from before this feature gets half degrees without user action."""
    configure_temperature_step(zone_device, "unknown")
    config_entry = await async_setup_daikin(hass, zone_device)

    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 0.5
    assert TEMPERATURE_STEP_STORAGE_KEY not in hass_storage
    assert not issue_registry.issues
    assert not hass.states.async_all("persistent_notification")
    zone_device.set.assert_not_awaited()


async def test_temperature_step_restored_after_reload(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """After a reload a whole-degree device does not need a new fallback."""
    configure_temperature_step(zone_device, "inferred")
    config_entry = await async_setup_daikin(hass, zone_device)
    await hass.async_block_till_done()
    # Stored by the first refresh
    assert hass_storage[TEMPERATURE_STEP_STORAGE_KEY]["data"]["status"] == "inferred"

    # A new pydaikin device object starts unknown, i.e. with half degrees
    configure_temperature_step(zone_device, "unknown")
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert zone_device.target_temperature_step_status == "inferred"
    assert hass.states.get(CLIMATE_ENTITY_ID).attributes[ATTR_TARGET_TEMP_STEP] == 1
    zone_device.set.assert_not_awaited()


async def test_temperature_step_removed_with_entry(
    hass: HomeAssistant, zone_device: ZoneDevice, hass_storage: dict[str, Any]
) -> None:
    """Deleting the config entry deletes its stored classification."""
    configure_temperature_step(zone_device, "unknown")
    hass_storage[TEMPERATURE_STEP_STORAGE_KEY] = temperature_step_record("inferred")
    config_entry = await async_setup_daikin(hass, zone_device)

    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert TEMPERATURE_STEP_STORAGE_KEY not in hass_storage
