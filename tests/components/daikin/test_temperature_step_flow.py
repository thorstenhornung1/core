"""End-to-end tests for the Daikin temperature step with a real pydaikin device."""

from collections.abc import Generator
from datetime import timedelta
import re
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from pydaikin.daikin_brp069 import DaikinBRP069
from pydaikin.exceptions import DaikinCommandRejected
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.daikin.const import DOMAIN, KEY_MAC, TIMEOUT_SEC
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

HOST = "192.0.2.20"
MAC = "aabbccddeeff"
STORAGE_KEY = "daikin.{}.temperature_step"

CONTROL = (
    "ret=OK,pow=1,mode=4,adv=,stemp={stemp},shum=0,dt1=25.0,dt2=M,dt3=25.0,"
    "dt4={stemp},dt5={stemp},dt7=25.0,dh1=0,dh2=50,dh3=0,dh4=0,dh5=0,dh7=0,"
    "dhh=50,b_mode=4,b_stemp={stemp},b_shum=0,alert=255,f_rate=A,b_f_rate=A,"
    "dfr1=A,dfr2=A,dfr3=A,dfr4=A,dfr5=A,dfr6=A,dfr7=A,dfrh=A,f_dir=0,b_f_dir=0,"
    "dfd1=0,dfd2=0,dfd3=0,dfd4=0,dfd5=0,dfd6=0,dfd7=0,dfdh=0"
)


class FakeBRP069:
    """A stateful BRP069 adapter behind the aiohttp client mock."""

    def __init__(self, *, half_degree: bool, ver: str | None = "1_14_88") -> None:
        """Initialize the fake adapter."""
        self.half_degree = half_degree
        self.ver = ver
        self.stemp = "22.0"
        self.writes: list[str] = []

    async def handle(
        self, method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        """Answer one request like the adapter."""
        return AiohttpClientMockResponse(method, url, text=self._reply(url))

    def _reply(self, url: URL) -> str:
        path = url.path
        if path == "/common/basic_info":
            ver = f"ver={self.ver}," if self.ver is not None else ""
            return (
                f"ret=OK,type=aircon,reg=eu,{ver}pow=1,err=0,name=%44%61%69%6b%69%6e,"
                f"method=home only,adp_kind=3,mac={MAC},adp_mode=run,en_hol=0"
            )
        if path == "/aircon/get_model_info":
            return (
                "ret=OK,model=0FC1,type=N,humd=0,s_humd=0,en_frate=1,en_fdir=1,s_fdir=3"
            )
        if path == "/aircon/get_sensor_info":
            return "ret=OK,htemp=22.0,hhum=-,otemp=8.0,err=0,cmpfreq=20"
        if path == "/common/get_datetime":
            return "ret=OK,sta=1,cur=2026/11/5 10:00:00,reg=eu,dst=1,zone=0"
        if path == "/aircon/get_control_info":
            return CONTROL.format(stemp=self.stemp)
        if path == "/aircon/set_control_info":
            stemp = url.query["stemp"]
            self.writes.append(stemp)
            if stemp.endswith(".5") and not self.half_degree:
                return "ret=PARAM NG"
            self.stemp = stemp
            return "ret=OK"
        return "ret=OK"


@pytest.fixture
def real_factory() -> Generator[None]:
    """Create a real pydaikin BRP069 for every config entry setup."""

    async def _factory(host: str, session: Any, **kwargs: Any) -> DaikinBRP069:
        device = DaikinBRP069(host, session=session)
        await device.init()
        return device

    with patch("homeassistant.components.daikin.DaikinFactory", side_effect=_factory):
        yield


def _register(aioclient_mock: AiohttpClientMocker, fake: FakeBRP069) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        re.compile(rf"http://{re.escape(HOST)}/.*"), side_effect=fake.handle
    )


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, data={CONF_HOST: HOST, KEY_MAC: MAC}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _climate(hass: HomeAssistant) -> str:
    return hass.states.async_entity_ids(CLIMATE_DOMAIN)[0]


async def _set_temperature(hass: HomeAssistant, temperature: float) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: _climate(hass), ATTR_TEMPERATURE: temperature},
        blocking=True,
    )


async def _reload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("real_factory")
async def test_inferred_whole_degree_survives_reload(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A rejected half degree shows the real setpoint, 1 °C, also after reload."""
    fake = FakeBRP069(half_degree=False)
    _register(aioclient_mock, fake)
    entry = await _setup(hass)
    assert hass.states.get(_climate(hass)).attributes[ATTR_TARGET_TEMP_STEP] == 0.5

    await _set_temperature(hass, 22.5)

    state = hass.states.get(_climate(hass))
    assert fake.writes == ["22.5", "23"]
    assert state.attributes[ATTR_TEMPERATURE] == 23
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == 1.0
    stored = hass_storage[STORAGE_KEY.format(entry.entry_id)]["data"]
    assert stored["status"] == "inferred"
    assert stored["device_mac"] == "aa:bb:cc:dd:ee:ff"

    await _reload(hass, entry)
    assert hass.states.get(_climate(hass)).attributes[ATTR_TARGET_TEMP_STEP] == 1.0


@pytest.mark.usefixtures("real_factory")
async def test_confirmed_half_degree_survives_reload(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """An accepted half degree is stored as confirmed and kept after reload."""
    fake = FakeBRP069(half_degree=True)
    _register(aioclient_mock, fake)
    entry = await _setup(hass)

    await _set_temperature(hass, 22.5)

    assert fake.writes == ["22.5"]
    assert hass.states.get(_climate(hass)).attributes[ATTR_TEMPERATURE] == 22.5
    key = STORAGE_KEY.format(entry.entry_id)
    assert hass_storage[key]["data"]["status"] == "confirmed"

    await _reload(hass, entry)
    device = entry.runtime_data.device
    assert device.target_temperature_step_status == "confirmed"
    assert hass.states.get(_climate(hass)).attributes[ATTR_TARGET_TEMP_STEP] == 0.5


@pytest.mark.usefixtures("real_factory")
async def test_rejection_without_fallback_learns_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Outside the fallback range a rejection fails and nothing is stored."""
    fake = FakeBRP069(half_degree=False)
    _register(aioclient_mock, fake)
    entry = await _setup(hass)

    # The integration does not translate pydaikin command errors yet
    with pytest.raises(DaikinCommandRejected):
        await _set_temperature(hass, 17.5)

    assert fake.writes == ["17.5"]
    assert entry.runtime_data.device.target_temperature_step_status == "unknown"
    assert STORAGE_KEY.format(entry.entry_id) not in hass_storage


@pytest.mark.usefixtures("real_factory")
async def test_write_error_is_retried_by_the_coordinator(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A write error Store swallows keeps the command working; a refresh retries."""
    fake = FakeBRP069(half_degree=False)
    _register(aioclient_mock, fake)
    entry = await _setup(hass)
    key = STORAGE_KEY.format(entry.entry_id)

    with patch.object(Store, "_async_write_data", side_effect=WriteError("full")):
        await _set_temperature(hass, 22.5)

    state = hass.states.get(_climate(hass))
    assert state.attributes[ATTR_TEMPERATURE] == 23
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == 1.0
    assert key not in hass_storage

    freezer.tick(timedelta(seconds=TIMEOUT_SEC + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass_storage[key]["data"]["status"] == "inferred"


@pytest.mark.usefixtures("real_factory")
async def test_firmware_change_invalidates_on_setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A stored result of other firmware is discarded before entities appear."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, data={CONF_HOST: HOST, KEY_MAC: MAC}
    )
    entry.add_to_hass(hass)
    key = STORAGE_KEY.format(entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "status": "inferred",
            "device_mac": "aa:bb:cc:dd:ee:ff",
            "firmware_version": "1_0_0",
        },
    }
    _register(aioclient_mock, FakeBRP069(half_degree=True, ver="1_14_88"))

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.device.target_temperature_step_status == "unknown"
    assert hass.states.get(_climate(hass)).attributes[ATTR_TARGET_TEMP_STEP] == 0.5
    assert key not in hass_storage


@pytest.mark.usefixtures("real_factory")
async def test_options_flow_reset(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The options menu keeps or resets a learned step without device I/O."""
    fake = FakeBRP069(half_degree=False)
    _register(aioclient_mock, fake)
    entry = await _setup(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "step_unknown"

    await _set_temperature(hass, 22.5)
    requests = aioclient_mock.call_count

    # Opening and keeping changes nothing
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "inferred"
    assert result["menu_options"] == ["keep", "reset"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "keep"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "step_kept"
    assert entry.runtime_data.device.target_temperature_step_status == "inferred"
    assert STORAGE_KEY.format(entry.entry_id) in hass_storage

    # A failed deletion is reported and changes nothing
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch.object(Store, "async_remove", side_effect=OSError("read-only")):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "reset"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reset_failed"
    assert entry.runtime_data.device.target_temperature_step_status == "inferred"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reset"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reset_successful"
    assert aioclient_mock.call_count == requests
    assert entry.runtime_data.device.target_temperature_step_status == "unknown"
    assert hass.states.get(_climate(hass)).attributes[ATTR_TARGET_TEMP_STEP] == 0.5
    assert STORAGE_KEY.format(entry.entry_id) not in hass_storage

    # The entry is unloaded while the menu is open
    await _set_temperature(hass, 22.5)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reset"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "step_not_loaded"


@pytest.mark.usefixtures("real_factory")
async def test_options_flow_confirmed_menu(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A confirmed half degree shows its own menu text."""
    _register(aioclient_mock, FakeBRP069(half_degree=True))
    entry = await _setup(hass)
    await _set_temperature(hass, 22.5)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "confirmed"


@pytest.mark.usefixtures("real_factory")
async def test_options_flow_not_loaded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The options flow explains that the integration must be loaded."""
    _register(aioclient_mock, FakeBRP069(half_degree=True))
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "step_not_loaded"


@pytest.mark.usefixtures("real_factory")
async def test_remove_entry_deletes_store(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Removing the config entry removes the learned step."""
    _register(aioclient_mock, FakeBRP069(half_degree=False))
    entry = await _setup(hass)
    await _set_temperature(hass, 22.5)
    key = STORAGE_KEY.format(entry.entry_id)
    assert key in hass_storage

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_zone_device_without_learning_has_no_reset(
    hass: HomeAssistant, zone_device: Any
) -> None:
    """Adapters without step learning keep their step and offer no reset."""
    zone_device.target_temperature_step = 1.0
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=zone_device.mac,
        data={CONF_HOST: "127.0.0.1", KEY_MAC: zone_device.mac},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.temperature_step is None
    for entity_id in hass.states.async_entity_ids(CLIMATE_DOMAIN):
        assert hass.states.get(entity_id).attributes[ATTR_TARGET_TEMP_STEP] == 1
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: "climate.daikin_test", ATTR_TEMPERATURE: 23},
        blocking=True,
    )
    zone_device.set.assert_awaited_once_with({"stemp": "23"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "step_fixed"
