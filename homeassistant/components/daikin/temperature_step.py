"""Keep the target temperature step pydaikin learns across restarts.

pydaikin learns whether a BRP069-family adapter accepts half degrees and
keeps the result in memory. This module stores it per config entry and
restores it on setup when the adapter reports the same MAC address and
firmware. They are a plausibility check, not an authenticated device
identity.
"""

import asyncio
import logging
import re
from typing import Any, TypedDict, TypeGuard

from pydaikin.daikin_base import Appliance
from pydaikin.daikin_brp069 import DaikinBRP069

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
LEARNED_STATUSES = ("confirmed", "inferred")
_MAX_IDENTIFIER_LENGTH = 128
_MAC = re.compile(r"[0-9a-fA-F]{12}")


class StepRecord(TypedDict):
    """A learned step and the device it was learned from."""

    status: str
    device_mac: str
    firmware_version: str


class DaikinStepResetError(Exception):
    """The stored temperature step could not be removed."""


def supports_step_learning(device: Appliance) -> TypeGuard[DaikinBRP069]:
    """Return True for adapters whose step pydaikin learns.

    AirBase inherits the status property from BRP069 but has a fixed
    whole-degree step, so the class default of 0.5 tells them apart.
    """
    return (
        isinstance(device, DaikinBRP069) and type(device).TARGET_TEMPERATURE_STEP == 0.5
    )


def _create_store(hass: HomeAssistant, entry_id: str) -> Store[StepRecord]:
    return Store(
        hass,
        STORAGE_VERSION,
        f"daikin.{entry_id}.temperature_step",
        private=True,
        atomic_writes=True,
    )


def _mac(value: Any) -> str | None:
    """Return a real MAC address (Appliance.mac falls back to the IP)."""
    if not isinstance(value, str):
        return None
    cleaned = value.replace(":", "").replace("-", "")
    return format_mac(cleaned) if _MAC.fullmatch(cleaned) else None


def _firmware(value: Any) -> str | None:
    """Return a printable firmware version of bounded length."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_IDENTIFIER_LENGTH or not value.isprintable():
        return None
    return value


def _identity(device: Appliance) -> tuple[str, str] | None:
    """Return MAC address and firmware, or None if either is unknown."""
    mac = _mac(device.values.get("mac"))
    firmware = _firmware(device.values.get("ver"))
    if mac is None or firmware is None:
        return None
    return mac, firmware


def _is_record(data: Any) -> TypeGuard[StepRecord]:
    return (
        isinstance(data, dict)
        and data.get("status") in LEARNED_STATUSES
        and all(
            isinstance(data.get(key), str) for key in ("device_mac", "firmware_version")
        )
    )


class DaikinTemperatureStep:
    """Store and restore the learned step of one config entry."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, device: DaikinBRP069
    ) -> None:
        """Initialize the store for a learning adapter."""
        self._hass = hass
        self._device = device
        self._store = _create_store(hass, entry_id)
        # Serializes device commands with saving and resetting the step
        self._lock = asyncio.Lock()
        self._closed = False
        # The record to keep, and the one verified in storage
        self._desired: StepRecord | None = None
        self._persisted: StepRecord | None = None

    @property
    def status(self) -> str:
        """Return the step status reported by pydaikin."""
        return self._device.target_temperature_step_status

    async def async_restore(self) -> None:
        """Restore the stored step if it belongs to this device.

        Runs once on setup, before the first refresh, while the identity
        pydaikin just read is fresh. pydaikin caches that identity for the
        lifetime of the device object, so it is not checked again later.
        """
        try:
            record = await self._store.async_load()
        except (HomeAssistantError, OSError) as err:
            _LOGGER.warning("Could not load the Daikin temperature step: %s", err)
            return
        if record is None:
            return
        identity = _identity(self._device)
        if identity is None:
            _LOGGER.debug("Daikin identity incomplete, not restoring the step")
            return
        if (
            not _is_record(record)
            or (
                record["device_mac"],
                record["firmware_version"],
            )
            != identity
        ):
            _LOGGER.info("Discarding the Daikin temperature step of another device")
            await self._async_remove_stale()
            return
        self._device.target_temperature_step_status = record["status"]
        self._desired = self._persisted = record

    async def async_set(self, settings: dict[str, Any]) -> None:
        """Send settings to the device, then store a newly learned step.

        A storage failure never fails the command; the save is retried with
        the next coordinator update. After unloading, nothing is sent.
        """
        async with self._lock:
            if self._closed:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="entry_unloaded"
                )
            try:
                await self._device.set(settings)
            finally:
                self._take_learned_status()
                await self._async_persist()

    async def async_retry(self) -> None:
        """Retry a pending save, without I/O when nothing is pending."""
        if self._desired == self._persisted:
            return
        async with self._lock:
            await self._async_persist()

    async def async_reset(self) -> None:
        """Forget the learned step without contacting the device."""
        async with self._lock:
            try:
                await self._store.async_remove()
            except OSError as err:
                raise DaikinStepResetError(str(err)) from err
            self._desired = self._persisted = None
            self._device.target_temperature_step_status = "unknown"

    async def async_close(self) -> None:
        """Wait for a running command, then refuse commands and saves."""
        async with self._lock:
            self._closed = True

    def _take_learned_status(self) -> None:
        status = self.status
        if status not in LEARNED_STATUSES or (
            self._desired is not None and self._desired["status"] == status
        ):
            return
        if (identity := _identity(self._device)) is None:
            _LOGGER.debug("Daikin identity incomplete, not storing the step")
            return
        self._desired = {
            "status": status,
            "device_mac": identity[0],
            "firmware_version": identity[1],
        }

    async def _async_persist(self) -> None:
        if self._closed or self._desired is None or self._desired == self._persisted:
            return
        desired = self._desired
        try:
            await self._store.async_save(desired)
            if self._hass.state is CoreState.stopping:
                # Only queued for the final write, so it cannot be verified
                return
            # Store logs write errors itself, so read the record back
            stored = await self._store.async_load()
        except (HomeAssistantError, OSError) as err:
            _LOGGER.warning("Could not save the Daikin temperature step: %s", err)
            return
        if stored == desired:
            self._persisted = desired
        else:
            _LOGGER.debug("Daikin temperature step not saved, retrying later")

    async def _async_remove_stale(self) -> None:
        try:
            await self._store.async_remove()
        except OSError as err:
            _LOGGER.warning("Could not remove the Daikin temperature step: %s", err)


async def async_remove_temperature_step(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the stored step of a removed config entry."""
    try:
        await _create_store(hass, entry_id).async_remove()
    except OSError as err:
        _LOGGER.warning("Could not remove the Daikin temperature step: %s", err)
