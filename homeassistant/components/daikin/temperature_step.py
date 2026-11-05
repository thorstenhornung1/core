"""Persist learned Daikin target temperature step between Home Assistant restarts."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
import logging
import re
from typing import TYPE_CHECKING, Any

from pydaikin.daikin_base import Appliance

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.storage import Store

if TYPE_CHECKING:
    from .coordinator import DaikinCoordinator

_LOGGER = logging.getLogger(__name__)
_IDENTITY_FIELDS = ("device_mac", "model", "firmware_version")
_LEARNED_STATUSES = ("confirmed", "inferred")


class DaikinStepResetError(Exception):
    """Unable to remove the persisted temperature step."""


def supports_step_learning(device: Appliance) -> bool:
    """Only BRP069-style devices with an optimistic half-degree step learn it."""
    return (
        hasattr(device, "target_temperature_step_status")
        and getattr(type(device), "TARGET_TEMPERATURE_STEP", None) == 0.5
    )


def _valid_mac(value: Any) -> str | None:
    """Return an actual MAC address, not Appliance.mac's IP fallback."""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[:-]", "", value)
    if len(cleaned) != 12 or re.fullmatch(r"[0-9a-fA-F]{12}", cleaned) is None:
        return None
    return format_mac(cleaned)


def _text_or_none(value: Any) -> str | None:
    """Discard missing or non-text device identifiers."""
    return value.strip() or None if isinstance(value, str) else None


class DaikinTemperatureStep:
    """Store pydaikin's learning status; never infer device capabilities in HA."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: Appliance
    ) -> None:
        """Initialize one per-config-entry persistence adapter."""
        self.hass = hass
        self.entry = entry
        self.device = device
        self.store = Store[dict[str, Any]](
            hass,
            1,
            f"daikin.{entry.entry_id}.temperature_step",
            atomic_writes=True,
        )
        self.lock = asyncio.Lock()
        self.closed = False
        self._record: dict[str, Any] | None = None
        self._coordinator: DaikinCoordinator | None = None
        self._unsub_identity: Callable[[], None] | None = None
        self._identity_task: asyncio.Task[None] | None = None

    @property
    def status(self) -> str:
        """Return the current status reported by pydaikin."""
        return self.device.target_temperature_step_status

    def _identity(self) -> dict[str, str | None]:
        """Read real identity fields, without Appliance.mac's IP fallback."""
        values = self.device.values
        return {
            "device_mac": _valid_mac(values.get("mac")),
            "model": _text_or_none(values.get("model")),
            "firmware_version": _text_or_none(values.get("ver")),
        }

    def _identity_changes(self) -> tuple[bool, dict[str, str]]:
        """Return mismatch and newly known fields in the current record."""
        if self._record is None:
            return False, {}
        current = self._identity()
        mismatch = any(
            self._record.get(key) is not None
            and value is not None
            and self._record[key] != value
            for key, value in current.items()
        )
        additions = {
            key: value
            for key, value in current.items()
            if self._record.get(key) is None and value is not None
        }
        return mismatch, additions

    async def async_restore(self) -> None:
        """Restore learned status before the first coordinator refresh."""
        try:
            data = await self.store.async_load()
        except Exception as err:
            _LOGGER.warning("Unable to load Daikin step detection: %s", err)
            return

        if not isinstance(data, dict) or data.get("status") not in _LEARNED_STATUSES:
            return

        self._record = data
        mismatch, _ = self._identity_changes()
        if mismatch:
            await self._async_invalidate_unlocked()
            return

        try:
            self.device.target_temperature_step_status = data["status"]
        except ValueError:
            _LOGGER.warning("Ignoring invalid saved Daikin step detection")
            self._record = None

    def bind_coordinator(self, coordinator: DaikinCoordinator) -> None:
        """Link updates after setup without exposing persistence to pydaikin."""
        self._coordinator = coordinator

    async def async_check_identity(self) -> None:
        """Validate a restored fingerprint and learn metadata as it arrives."""
        async with self.lock:
            if self.closed or self._record is None:
                return
            mismatch, additions = self._identity_changes()
            if mismatch:
                await self._async_invalidate_locked()
            elif additions:
                record = {**self._record, **additions}
                await self._async_save_locked(record)
            if self._record is None or all(
                self._record.get(field) is not None for field in _IDENTITY_FIELDS
            ):
                self._stop_watching()

    async def _async_invalidate_unlocked(self) -> None:
        """Invalidate an old fingerprint during startup, without a lock."""
        await self._async_remove_for_invalidation()
        self.device.target_temperature_step_status = "unknown"

    async def _async_invalidate_locked(self) -> None:
        """Invalidate a mismatched fingerprint and update any active entity."""
        await self._async_invalidate_unlocked()
        if self._coordinator is not None:
            self._coordinator.async_update_listeners()

    async def _async_remove_for_invalidation(self) -> None:
        """Remove a stale record without interrupting device operations."""
        try:
            await self.store.async_remove()
        except Exception as err:
            _LOGGER.warning("Unable to remove outdated Daikin step detection: %s", err)
            # Keep the fingerprint to retry its invalidation on a later update.
            return
        _LOGGER.info("Discarding Daikin step detection after device identity change")
        self._record = None

    async def _async_save_locked(self, record: dict[str, Any]) -> None:
        """Submit a learned result to HA's store, isolating write failures."""
        try:
            await self.store.async_save(record)
        except Exception as err:
            _LOGGER.warning("Unable to save Daikin step detection: %s", err)
        # Store may log disk errors internally without raising to its caller.
        self._record = record

    async def _async_save_if_changed_locked(self) -> None:
        """Save a newly learned state immediately after the device transaction."""
        status = self.status
        if status not in _LEARNED_STATUSES:
            return
        identity = self._identity()
        record = {
            "status": status,
            **identity,
            "learned_at": datetime.now(UTC).isoformat(),
        }
        if self._record is not None:
            if self._record.get("status") == status:
                # Missing identity fields may become available after learning.
                mismatch, additions = self._identity_changes()
                if not mismatch and not additions:
                    return
                if not mismatch:
                    record = {**self._record, **additions}
            else:
                for key in _IDENTITY_FIELDS:
                    if record[key] is None:
                        record[key] = self._record.get(key)
        await self._async_save_locked(record)
        self.watch_identity()

    @asynccontextmanager
    async def async_set_context(self) -> AsyncIterator[None]:
        """Serialize a device write, step persistence, and manual reset."""
        async with self.lock:
            if self.closed:
                raise HomeAssistantError("Daikin config entry is unloading")
            try:
                yield
            finally:
                await self._async_save_if_changed_locked()

    async def async_reset(self) -> None:
        """Reset only after a confirmed storage deletion; never contact device."""
        async with self.lock:
            if self.closed:
                raise DaikinStepResetError("Daikin config entry is unloading")
            try:
                await self.store.async_remove()
            except Exception as err:
                _LOGGER.warning("Unable to reset Daikin step detection: %s", err)
                raise DaikinStepResetError from err
            self._record = None
            self.device.target_temperature_step_status = "unknown"
            self._stop_watching()
            if self._coordinator is not None:
                self._coordinator.async_update_listeners()

    @callback
    def watch_identity(self) -> None:
        """Track missing metadata until it is available, without device I/O."""
        if (
            self.closed
            or self._record is None
            or self._coordinator is None
            or self._unsub_identity is not None
            or all(self._record.get(key) is not None for key in _IDENTITY_FIELDS)
        ):
            return
        self._unsub_identity = self._coordinator.async_add_listener(
            self._identity_updated
        )
        self.entry.async_on_unload(self._stop_watching)

    @callback
    def _identity_updated(self) -> None:
        """Schedule at most one background operation when fingerprint changes."""
        if self.closed or self._record is None:
            self._stop_watching()
            return
        if self._identity_task is not None and not self._identity_task.done():
            return
        mismatch, additions = self._identity_changes()
        if not mismatch and not additions:
            return
        self._identity_task = self.entry.async_create_background_task(
            self.hass,
            self.async_check_identity(),
            "daikin temperature step identity",
        )

    @callback
    def _stop_watching(self) -> None:
        """Unregister the metadata watcher, also on HA config entry unload."""
        if self._unsub_identity is not None:
            self._unsub_identity()
            self._unsub_identity = None

    async def async_close(self) -> None:
        """Drain command writes and stop all metadata work before unloading."""
        self._stop_watching()
        task = self._identity_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        async with self.lock:
            self.closed = True


async def async_remove_step_store(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove persisted step state even if config entry setup previously failed."""
    store = Store[dict[str, Any]](
        hass, 1, f"daikin.{entry.entry_id}.temperature_step", atomic_writes=True
    )
    try:
        await store.async_remove()
    except Exception as err:
        _LOGGER.warning("Unable to remove Daikin step storage: %s", err)
