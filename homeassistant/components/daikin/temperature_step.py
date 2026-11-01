"""Persist the target temperature step pydaikin learns for a Daikin device."""

import asyncio
import logging
from typing import Any

from pydaikin.daikin_base import Appliance

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import UnsupportedStorageVersionError
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

STATUS_UNKNOWN = "unknown"
STATUS_CONFIRMED = "confirmed"
STATUS_INFERRED = "inferred"
STATUSES = (STATUS_UNKNOWN, STATUS_CONFIRMED, STATUS_INFERRED)
LEARNED_STATUSES = (STATUS_CONFIRMED, STATUS_INFERRED)

HALF_DEGREE = 0.5
WHOLE_DEGREE = 1.0


def step_status(device: Appliance) -> str | None:
    """Return the pydaikin step status, None if the device does not detect it."""
    if getattr(device, "detects_target_temperature_step", None) is False:
        return None
    status = getattr(device, "target_temperature_step_status", None)
    return status if status in STATUSES else None


def control_step(device: Appliance) -> float:
    """Return the step pydaikin reports, whole degrees without step status.

    BRP069/BRP072C adapters report half degrees until a rejected half degree
    made pydaikin fall back to a whole degree.
    """
    if step_status(device) is None:
        return WHOLE_DEGREE
    return device.target_temperature_step


def _storage_key(entry_id: str) -> str:
    """Return the storage key for a config entry."""
    return f"{DOMAIN}.{entry_id}.temperature_step"


async def async_remove_temperature_step(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the stored classification of a config entry."""
    await Store[dict[str, Any]](
        hass, STORAGE_VERSION, _storage_key(entry_id)
    ).async_remove()


class DaikinTemperatureStep:
    """Keep the learned step status of a device in sync with storage."""

    def __init__(self, hass: HomeAssistant, entry_id: str, device: Appliance) -> None:
        """Initialize the temperature step storage."""
        self._device = device
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, _storage_key(entry_id)
        )
        self._stored_status: str | None = None
        self._lock = asyncio.Lock()

    @property
    def status(self) -> str | None:
        """Return the current pydaikin status, None if not supported."""
        return step_status(self._device)

    def _identity(self) -> dict[str, str | None]:
        """Return the identifiers a stored classification must match."""
        values = self._device.values
        return {
            "device_mac": format_mac(self._device.mac),
            "model": values.get("model"),
            "firmware_version": values.get("ver"),
        }

    async def async_restore(self) -> None:
        """Restore a stored classification into pydaikin.

        Runs before the entities are added. Stored data that does not match
        the device is removed, the device then keeps its own status.
        """
        try:
            record = await self._store.async_load()
        except UnsupportedStorageVersionError as err:
            _LOGGER.warning("Ignoring stored Daikin temperature step: %s", err)
            return
        if record is None:
            return
        async with self._lock:
            if self._is_valid(record) and self._apply(record["status"]):
                self._stored_status = record["status"]
                return
            _LOGGER.debug(
                "Discarding stored Daikin temperature step, it does not apply"
            )
            await self._store.async_remove()

    def _is_valid(self, record: Any) -> bool:
        """Return True if the record belongs to this device."""
        return (
            isinstance(record, dict)
            and record.get("status") in LEARNED_STATUSES
            and all(record.get(key) == value for key, value in self._identity().items())
        )

    def _apply(self, status: str) -> bool:
        """Set the status in pydaikin, return False if the device rejects it."""
        if self.status is None:
            return False
        try:
            self._device.target_temperature_step_status = status
        except ValueError:
            return False
        if status == STATUS_CONFIRMED and control_step(self._device) != HALF_DEGREE:
            self._device.target_temperature_step_status = STATUS_UNKNOWN
            return False
        return True

    async def async_save_learned(self) -> None:
        """Store the status pydaikin learned since the last save."""
        async with self._lock:
            status = self.status
            if status not in LEARNED_STATUSES or status == self._stored_status:
                return
            await self._store.async_save(
                {
                    "status": status,
                    **self._identity(),
                    "last_verified": dt_util.utcnow().isoformat(),
                }
            )
            self._stored_status = status

    async def async_reset(self) -> None:
        """Forget the learned classification without contacting the device."""
        async with self._lock:
            if self.status is not None:
                self._device.target_temperature_step_status = STATUS_UNKNOWN
            await self._store.async_remove()
            self._stored_status = None
